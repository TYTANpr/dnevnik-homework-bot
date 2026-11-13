import argparse
import base64
import hashlib
import html
import io
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import shutil
import subprocess
import tempfile
try:
    import pymupdf
except ImportError:
    pymupdf = None
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://dnevnik2.petersburgedu.ru"
LESSONS_URL = f"{BASE_URL}/api/journal/lesson/list-by-education"
FILE_DOWNLOAD_URL = f"{BASE_URL}/api/filekit/file/download"
PROFILE_URL = f"{BASE_URL}/api/journal/person/related-child-list"

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
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def extract_jwt_payload(token: str) -> dict:
    """Извлекает полезную нагрузку из JWT (включая ключ esk/sso-key)."""
    try:
        parts = token.strip().split(".")
        if len(parts) >= 2:
            padding = "=" * ((4 - len(parts[1]) % 4) % 4)
            raw = base64.urlsafe_b64decode(parts[1] + padding)
            return json.loads(raw)
    except Exception:
        pass
    return {}


def load_active_tokens() -> tuple[str, str | None]:
    """Загружает актуальный X-JWT-Token и sso-key."""
    jwt_token = ENV_JWT_TOKEN
    sso_key = None

    if TOKEN_CACHE_FILE.exists():
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("env") == ENV_JWT_TOKEN and data.get("jwt"):
                jwt_token = data["jwt"].strip()
                sso_key = data.get("sso_key")
        except Exception:
            pass

    if not sso_key:
        payload = extract_jwt_payload(jwt_token)
        sso_key = payload.get("esk") or payload.get("sso-key")

    return jwt_token, sso_key


def save_active_tokens(jwt_token: str, sso_key: str | None = None) -> None:
    if not jwt_token:
        return
    if not sso_key:
        payload = extract_jwt_payload(jwt_token)
        sso_key = payload.get("esk") or payload.get("sso-key")

    data = {
        "env": ENV_JWT_TOKEN,
        "jwt": jwt_token.strip(),
        "sso_key": sso_key.strip() if sso_key else None,
        "updated_at": datetime.now().isoformat(),
    }
    TOKEN_CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
            "Connection": "close",
        }
    )

    jwt_token, sso_key = load_active_tokens()
    if jwt_token:
        session.cookies.set("X-JWT-Token", jwt_token, domain="dnevnik2.petersburgedu.ru")
    if sso_key:
        session.cookies.set("sso-key", sso_key, domain="dnevnik2.petersburgedu.ru")

    return session


def update_tokens_from_response(resp: requests.Response) -> None:
    new_jwt = resp.cookies.get("X-JWT-Token")
    new_sso = resp.cookies.get("sso-key")

    if new_jwt or new_sso:
        cur_jwt, cur_sso = load_active_tokens()
        save_active_tokens(new_jwt or cur_jwt, new_sso or cur_sso)


def api_request(
    session: requests.Session, url: str, params=None, stream: bool = False
) -> requests.Response:
    resp = session.get(url, params=params, timeout=25, stream=stream)
    update_tokens_from_response(resp)

    # Если токен начал устаревать, сервер возвращает 401 со свежим Set-Cookie
    if resp.status_code == 401:
        new_jwt = resp.cookies.get("X-JWT-Token")
        if new_jwt:
            print("Обнаружен свежий X-JWT-Token от сервера, повторяю запрос...")
            session.cookies.set("X-JWT-Token", new_jwt, domain="dnevnik2.petersburgedu.ru")
            save_active_tokens(new_jwt)
            resp.close()
            resp = session.get(url, params=params, timeout=25, stream=stream)
            update_tokens_from_response(resp)

    return resp


def tg_request(method: str, data: dict, files=None) -> dict:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    tg_sess = requests.Session()
    if TG_PROXY:
        tg_sess.trust_env = False
        tg_sess.proxies = {"http": TG_PROXY, "https": TG_PROXY}

    resp = tg_sess.post(url, data=data, files=files, timeout=60)
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
        resp = api_request(session, LESSONS_URL, params=params)

        if resp.status_code == 401:
            raise PermissionError(
                "Ошибка 401: сессия дневника истекла. Обнови DNEVNIK_JWT_TOKEN в GitHub Secrets."
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
    resp = api_request(session, FILE_DOWNLOAD_URL, params={"p_uuid": uuid})
    resp.raise_for_status()
    return resp.content


def pdf_to_png_pages(pdf_bytes: bytes, dpi: int = 200) -> list[bytes]:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            pages.append(pix.tobytes("png"))
    finally:
        doc.close()
    return pages


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
        lines.append("<b>ДЗ:</b> на фото 👇")

    message_text = "\n".join(lines)

    base_payload = {"chat_id": TG_CHAT_ID}
    if TG_TOPIC_ID:
        base_payload["message_thread_id"] = TG_TOPIC_ID

    photo_items: list[tuple[str, bytes]] = []
    other_files: list[tuple[str, bytes]] = []

    for f in files:
        file_name = f.get("file_name") or "file"
        file_type = (f.get("file_type") or "").lower()
        ext = Path(file_name).suffix.lower()
        file_bytes = download_file(session, f["uuid"])

        if ext == ".pdf" or file_type == "application/pdf":
            try:
                pages = pdf_to_png_pages(file_bytes, dpi=200)
                if pages:
                    for idx, png_bytes in enumerate(pages, start=1):
                        photo_items.append((f"{Path(file_name).stem}_p{idx}.png", png_bytes))
                    continue
            except Exception as err:
                print(f"Не удалось отрендерить PDF {file_name}, отправляю файлом: {err}")
            other_files.append((file_name, file_bytes))
        elif ext in IMAGE_EXTENSIONS or file_type.startswith("image/"):
            photo_items.append((file_name, file_bytes))
        else:
            other_files.append((file_name, file_bytes))

    caption_for_media = message_text if len(message_text) <= 950 else None
    if not photo_items or caption_for_media is None:
        tg_request(
            "sendMessage",
            data={
                **base_payload,
                "text": message_text,
                "parse_mode": "HTML",
            },
        )

    for i in range(0, len(photo_items), 10):
        chunk = photo_items[i : i + 10]
        chunk_caption = caption_for_media if i == 0 else None

        if len(chunk) == 1:
            name, img_bytes = chunk[0]
            payload = {**base_payload}
            if chunk_caption:
                payload["caption"] = chunk_caption
                payload["parse_mode"] = "HTML"
            tg_request(
                "sendPhoto",
                data=payload,
                files={"photo": (name, io.BytesIO(img_bytes), "image/png")},
            )
        else:
            media = []
            files_payload = {}
            for idx, (name, img_bytes) in enumerate(chunk):
                attach_name = f"photo_{idx}"
                item = {"type": "photo", "media": f"attach://{attach_name}"}
                if idx == 0 and chunk_caption:
                    item["caption"] = chunk_caption
                    item["parse_mode"] = "HTML"
                media.append(item)
                files_payload[attach_name] = (name, io.BytesIO(img_bytes), "image/png")

            tg_request(
                "sendMediaGroup",
                data={**base_payload, "media": json.dumps(media, ensure_ascii=False)},
                files=files_payload,
            )

        if i + 10 < len(photo_items):
            time.sleep(0.5)

    for name, file_bytes in other_files:
        time.sleep(0.5)
        tg_request(
            "sendDocument",
            data=base_payload,
            files={"document": (name, io.BytesIO(file_bytes))},
        )


def run_once() -> None:
    jwt_token, _ = load_active_tokens()
    if not TG_BOT_TOKEN or not TG_CHAT_ID or not EDUCATION_ID or not jwt_token:
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
