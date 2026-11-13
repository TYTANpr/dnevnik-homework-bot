import argparse
import hashlib
import html
import io
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://dnevnik2.petersburgedu.ru"
LESSONS_URL = f"{BASE_URL}/api/journal/lesson/list-by-education"
FILE_DOWNLOAD_URL = f"{BASE_URL}/api/filekit/file/download"

# Храним состояние вне рабочей папки раннера, чтобы оно не стиралось
STATE_DIR = Path.home() / ".dnevnik_bot"
STATE_DIR.mkdir(exist_ok=True)
SENT_FILE = STATE_DIR / "sent.json"
TOKEN_CACHE_FILE = STATE_DIR / "token_cache.json"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
TG_TOPIC_ID = os.getenv("TG_TOPIC_ID", "").strip()
TG_ADMIN_ID = os.getenv("TG_ADMIN_ID", "").strip()
TG_PROXY = os.getenv("TG_PROXY", "").strip()

EDUCATION_ID = os.getenv("EDUCATION_ID", "").strip()
ENV_JWT_TOKEN = os.getenv("DNEVNIK_JWT_TOKEN", "").strip()

TARGET_SUBJECTS = {
    s.strip().lower()
    for s in os.getenv(
        "TARGET_SUBJECTS", "Алгебра,Геометрия,Вероятность и статистика"
    ).split(",")
    if s.strip()
}

LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "7"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))


def load_jwt_token() -> str:
    if TOKEN_CACHE_FILE.exists():
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            # Если секрет на GitHub обновили вручную — берём новый секрет, а не старый кэш
            if data.get("env") == ENV_JWT_TOKEN and data.get("active"):
                return data["active"].strip()
        except Exception:
            pass
    return ENV_JWT_TOKEN


def save_jwt_token(token: str) -> None:
    if token:
        TOKEN_CACHE_FILE.write_text(
            json.dumps(
                {"env": ENV_JWT_TOKEN, "active": token.strip()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def get_cookie_jwt(session: requests.Session) -> str | None:
    for c in session.cookies:
        if c.name == "X-JWT-Token" and c.value:
            return c.value
    return None


def load_sent_hashes() -> set[str]:
    if not SENT_FILE.exists():
        return set()
    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def save_sent_hashes(hashes: set[str]) -> None:
    recent = sorted(hashes)[-1000:]
    SENT_FILE.write_text(
        json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_session() -> requests.Session:
    session = requests.Session()
    # Дневник всегда идёт напрямую, в обход системного прокси Windows
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{BASE_URL}/schedule",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    token = load_jwt_token()
    if token:
        session.cookies.set("X-JWT-Token", token, domain="dnevnik2.petersburgedu.ru")
    return session


def request_with_token_refresh(
    session: requests.Session, url: str, params=None, stream: bool = False
) -> requests.Response:
    old_token = get_cookie_jwt(session)
    resp = session.get(url, params=params, timeout=25, stream=stream)

    if resp.status_code == 401:
        new_token = resp.cookies.get("X-JWT-Token") or get_cookie_jwt(session)
        if new_token and new_token != old_token:
            save_jwt_token(new_token)
            resp.close()
            resp = session.get(url, params=params, timeout=25, stream=stream)

    latest_token = resp.cookies.get("X-JWT-Token") or get_cookie_jwt(session)
    if latest_token and latest_token != old_token:
        save_jwt_token(latest_token)

    return resp


def tg_request(method: str, data: dict, files=None) -> dict:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    tg_sess = requests.Session()
    if TG_PROXY:
        tg_sess.trust_env = False
        tg_sess.proxies = {"http": TG_PROXY, "https": TG_PROXY}

    resp = tg_sess.post(url, data=data, files=files, timeout=40)
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error ({method}): {result}")
    return result


def notify_admin(text: str) -> None:
    if not TG_ADMIN_ID or not TG_BOT_TOKEN:
        return
    try:
        tg_request(
            "sendMessage",
            {"chat_id": TG_ADMIN_ID, "text": text, "parse_mode": "HTML"},
        )
    except Exception as e:
        print(f"Не удалось уведомить админа: {e}")


def fetch_lessons(session: requests.Session) -> list[dict]:
    now = datetime.now()
    date_from = (now - timedelta(days=1)).strftime("%d.%m.%Y 00:00:00")
    date_to = (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%d.%m.%Y 23:59:59")

    all_lessons = []
    page = 1

    while page <= 20:
        params = [
            ("p_page", str(page)),
            ("p_datetime_from", date_from),
            ("p_datetime_to", date_to),
            ("p_educations[]", EDUCATION_ID),
        ]
        resp = request_with_token_refresh(session, LESSONS_URL, params=params)

        if resp.status_code == 401:
            raise PermissionError(
                "Ошибка 401: токен X-JWT-Token истёк. Обнови DNEVNIK_JWT_TOKEN в GitHub Secrets."
            )
        resp.raise_for_status()

        payload = resp.json()
        data = payload.get("data") or {}
        items = data.get("items") or []

        if not items:
            break

        all_lessons.extend(items)

        meta = data.get("meta") or payload.get("meta") or {}
        if meta.get("last_page") and page >= int(meta["last_page"]):
            break
        if data.get("before") == 0:
            break

        page += 1
        time.sleep(0.4)

    return all_lessons


def download_file(session: requests.Session, uuid: str) -> bytes:
    resp = request_with_token_refresh(
        session, FILE_DOWNLOAD_URL, params={"p_uuid": uuid}
    )
    resp.raise_for_status()
    return resp.content


def make_homework_hash(
    dt: str, subject: str, texts: list[str], file_uuids: list[str]
) -> str:
    raw = json.dumps(
        {
            "dt": dt,
            "subject": subject,
            "texts": texts,
            "files": sorted(file_uuids),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def send_homework_to_tg(
    session: requests.Session,
    dt_str: str,
    subject: str,
    topic: str | None,
    texts: list[str],
    files: list[dict],
) -> None:
    try:
        date_short = datetime.strptime(dt_str, "%d.%m.%Y %H:%M:%S").strftime("%d.%m")
    except ValueError:
        date_short = dt_str[:10]

    lines = [f"📘 <b>{html.escape(subject)}</b> ({html.escape(date_short)})"]
    if topic:
        lines.append(f"<b>Тема:</b> {html.escape(topic)}")

    if texts:
        joined = "\n".join(texts)
        lines.append(f"<b>ДЗ:</b> {html.escape(joined)}")
    elif files:
        lines.append("<b>ДЗ:</b> во вложении 👇")

    message_text = "\n".join(lines)

    base_payload = {"chat_id": TG_CHAT_ID}
    if TG_TOPIC_ID:
        base_payload["message_thread_id"] = TG_TOPIC_ID

    if len(files) == 1 and len(message_text) <= 950:
        f = files[0]
        file_bytes = download_file(session, f["uuid"])
        buf = io.BytesIO(file_bytes)
        buf.name = f.get("file_name") or "homework.pdf"

        tg_request(
            "sendDocument",
            data={
                **base_payload,
                "caption": message_text,
                "parse_mode": "HTML",
            },
            files={"document": (buf.name, buf)},
        )
        return

    tg_request(
        "sendMessage",
        data={
            **base_payload,
            "text": message_text,
            "parse_mode": "HTML",
        },
    )

    for f in files:
        time.sleep(0.5)
        file_bytes = download_file(session, f["uuid"])
        buf = io.BytesIO(file_bytes)
        buf.name = f.get("file_name") or "attachment"
        tg_request(
            "sendDocument",
            data=base_payload,
            files={"document": (buf.name, buf)},
        )


def run_once() -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not EDUCATION_ID or not load_jwt_token():
        raise ValueError("Проверь переменные: не заполнены обязательные секреты!")

    session = build_session()
    sent_hashes = load_sent_hashes()

    try:
        lessons = fetch_lessons(session)
    except PermissionError as e:
        print(e)
        notify_admin(f"⚠️ <b>Бот ДЗ:</b> {html.escape(str(e))}")
        raise

    new_count = 0

    for lesson in lessons:
        subject = (lesson.get("subject_name") or "").strip()
        if subject.lower() not in TARGET_SUBJECTS:
            continue

        tasks = lesson.get("tasks") or []
        hw_tasks = [
            t for t in tasks if t.get("task_kind_code") in ("homework", None)
        ]
        if not hw_tasks:
            continue

        texts = []
        files_by_uuid = {}

        for task in hw_tasks:
            t_name = (task.get("task_name") or "").strip()
            if t_name and t_name not in texts:
                texts.append(t_name)

            for f in task.get("files") or []:
                uuid = f.get("uuid")
                if uuid and uuid not in files_by_uuid:
                    files_by_uuid[uuid] = f

        files = list(files_by_uuid.values())
        if not texts and not files:
            continue

        dt_from = lesson.get("datetime_from") or ""
        hw_hash = make_homework_hash(dt_from, subject, texts, list(files_by_uuid.keys()))

        if hw_hash in sent_hashes:
            continue

        topic = (lesson.get("content_name") or "").strip() or None
        print(f"Отправляю новое ДЗ: {subject} ({dt_from})")

        send_homework_to_tg(session, dt_from, subject, topic, texts, files)
        sent_hashes.add(hw_hash)
        save_sent_hashes(sent_hashes)
        new_count += 1
        time.sleep(0.8)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Готово. Новых ДЗ: {new_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Работать постоянно в цикле каждые CHECK_INTERVAL_MINUTES минут",
    )
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                run_once()
            except Exception as err:
                print(f"Ошибка цикла: {err}")
                notify_admin(f"❌ Ошибка бота ДЗ: <code>{html.escape(str(err))}</code>")
            time.sleep(CHECK_INTERVAL_MINUTES * 60)
    else:
        run_once()
