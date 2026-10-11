import os
import re
import json
import sqlite3
import asyncio
import logging
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_IDS = {
    item.strip()
    for item in os.getenv("TELEGRAM_ALLOWED_IDS", "").split(",")
    if item.strip()
}

API_BASE_URL = os.getenv("API_BASE_URL", "https://api.mtccenters.com").rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"
)
DATABASE_PATH = os.getenv("DATABASE_PATH", "sync_state.sqlite3")
POLL_INTERVAL_SECONDS = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "1800")))
PUBLISHED_ON_CREATE = os.getenv("PUBLISHED_ON_CREATE", "false").lower() in {
    "1", "true", "yes", "on"
}
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("drive-sync-bot")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_ALLOWED_IDS:
        missing.append("TELEGRAM_ALLOWED_IDS")
    if not ADMIN_EMAIL:
        missing.append("ADMIN_EMAIL")
    if not ADMIN_PASSWORD:
        missing.append("ADMIN_PASSWORD")
    if missing:
        raise RuntimeError("Missing required .env settings: " + ", ".join(missing))

    if not Path(GOOGLE_SERVICE_ACCOUNT_FILE).is_file():
        raise RuntimeError(
            f"Google service-account JSON not found: {GOOGLE_SERVICE_ACCOUNT_FILE}"
        )


def extract_drive_id(value: str) -> str:
    """Accept a Drive file/folder ID or a Google Drive URL."""
    value = (value or "").strip()
    if not value:
        raise ValueError("A Google Drive URL or ID is required.")

    if "://" not in value and "/" not in value and " " not in value:
        return value

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if not (host.endswith("google.com") or host.endswith("googleusercontent.com")):
        raise ValueError("The URL must be from Google Drive.")

    match = re.search(
        r"/(?:folders|file/d|document/d|spreadsheets/d|presentation/d)/([^/?]+)",
        parsed.path,
    )
    if match:
        return match.group(1)

    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id:
        return query_id

    raise ValueError("Could not extract an ID from the Google Drive URL.")


def drive_client():
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_children(service, folder_id: str) -> list[dict]:
    results = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=1000,
            pageToken=page_token,
            orderBy="name",
            fields=(
                "nextPageToken,files(id,name,mimeType,size,webViewLink,"
                "modifiedTime,resourceKey,parents,trashed)"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")

        if not page_token:
            return results


def get_drive_file(service, file_id: str) -> dict:
    return service.files().get(
        fileId=file_id,
        fields=(
            "id,name,mimeType,size,webViewLink,modifiedTime,"
            "resourceKey,parents,trashed"
        ),
        supportsAllDrives=True,
    ).execute()


def make_drive_url(file_info: dict) -> str:
    file_id = file_info["id"]
    url = file_info.get("webViewLink") or (
        f"https://drive.google.com/file/d/{file_id}/view"
    )
    resource_key = file_info.get("resourceKey")

    if resource_key and "resourcekey=" not in url.lower():
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}resourcekey={resource_key}"

    return url


VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm",
    ".mpeg", ".mpg", ".3gp",
}


def is_video(file_info: dict) -> bool:
    mime = (file_info.get("mimeType") or "").lower()
    suffix = Path(file_info.get("name", "")).suffix.lower()
    return mime.startswith("video/") or suffix in VIDEO_EXTENSIONS


def resource_type(file_info: dict) -> str:
    """
    Return enum values accepted by the backend:
    VIDEO, PDF, DOCUMENT, ZIP, LINK, RAR.
    Videos use the dedicated drive-stream endpoint.
    """
    name = (file_info.get("name") or "").lower()
    mime = (file_info.get("mimeType") or "").lower()
    suffix = Path(name).suffix.lower()

    if is_video(file_info):
        return "VIDEO"

    if suffix == ".pdf" or mime == "application/pdf":
        return "PDF"

    if suffix == ".zip" or mime in {
        "application/zip",
        "application/x-zip-compressed",
        "application/x-zip",
    }:
        return "ZIP"

    if suffix == ".rar" or mime in {
        "application/vnd.rar",
        "application/x-rar-compressed",
        "application/x-rar",
    }:
        return "RAR"

    return "DOCUMENT"


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and str(user.id) in TELEGRAM_ALLOWED_IDS)


async def reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


# ---------------------------------------------------------------------------
# SQLite state and local de-duplication
# ---------------------------------------------------------------------------

def db_connect():
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                batch_id TEXT PRIMARY KEY,
                folder_id TEXT NOT NULL,
                folder_url TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_status TEXT,
                last_error TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lectures (
                batch_id TEXT NOT NULL,
                folder_id TEXT NOT NULL,
                lecture_id TEXT NOT NULL,
                title TEXT NOT NULL,
                PRIMARY KEY (batch_id, folder_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS resources (
                batch_id TEXT NOT NULL,
                folder_id TEXT NOT NULL,
                drive_file_id TEXT NOT NULL,
                resource_id TEXT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (batch_id, folder_id, drive_file_id)
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(subscriptions)"
            ).fetchall()
        }

        if "last_sync_at" not in columns:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_sync_at TEXT"
            )

        if "last_status" not in columns:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_status TEXT"
            )

        if "last_error" not in columns:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN last_error TEXT"
            )


def get_subscription(batch_id: str):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()


def active_subscriptions():
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE active = 1 ORDER BY created_at"
        ).fetchall()


def save_subscription(batch_id: str, folder_id: str, folder_url: str) -> None:
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO subscriptions
                (batch_id, folder_id, folder_url, active, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                folder_id = excluded.folder_id,
                folder_url = excluded.folder_url,
                active = 1,
                last_error = NULL
        """, (batch_id, folder_id, folder_url, utc_now()))


def set_subscription_status(
    batch_id: str,
    status: str,
    error: str | None = None,
) -> None:
    with db_connect() as conn:
        conn.execute("""
            UPDATE subscriptions
            SET last_sync_at = ?, last_status = ?, last_error = ?
            WHERE batch_id = ?
        """, (utc_now(), status, error, batch_id))


def stop_subscription(batch_id: str) -> bool:
    with db_connect() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET active = 0 WHERE batch_id = ?",
            (batch_id,),
        )
        return cursor.rowcount > 0


def get_saved_lecture_id(batch_id: str, folder_id: str):
    with db_connect() as conn:
        row = conn.execute("""
            SELECT lecture_id FROM lectures
            WHERE batch_id = ? AND folder_id = ?
        """, (batch_id, folder_id)).fetchone()

        return row["lecture_id"] if row else None


def save_lecture(
    batch_id: str,
    folder_id: str,
    lecture_id: str,
    title: str,
) -> None:
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO lectures (batch_id, folder_id, lecture_id, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(batch_id, folder_id) DO UPDATE SET
                lecture_id = excluded.lecture_id,
                title = excluded.title
        """, (batch_id, folder_id, str(lecture_id), title))


def resource_already_saved(
    batch_id: str,
    folder_id: str,
    drive_file_id: str,
) -> bool:
    with db_connect() as conn:
        return conn.execute("""
            SELECT 1 FROM resources
            WHERE batch_id = ? AND folder_id = ? AND drive_file_id = ?
        """, (batch_id, folder_id, drive_file_id)).fetchone() is not None


def save_resource(
    batch_id: str,
    folder_id: str,
    drive_file_id: str,
    resource_id: str | None,
    name: str,
) -> None:
    with db_connect() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO resources
                (batch_id, folder_id, drive_file_id, resource_id, name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            batch_id,
            folder_id,
            drive_file_id,
            str(resource_id) if resource_id is not None else None,
            name,
            utc_now(),
        ))


# ---------------------------------------------------------------------------
# Backend API
# ---------------------------------------------------------------------------

class BackendAPI:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self.token = None
        self.login_lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def login(self):
        async with self.login_lock:
            response = await self.client.post(
                "/auth/login",
                json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("token")
            role = str(data.get("role", "")).upper()

            if not token:
                raise RuntimeError(f"Login response has no token: {data}")

            if role and role != "ADMIN":
                raise RuntimeError(
                    f"Backend account must have ADMIN role; got {role}"
                )

            self.token = token
            log.info("Logged in to backend as admin")

    async def request(self, method: str, path: str, **kwargs):
        if not self.token:
            await self.login()

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.token}"

        response = await self.client.request(
            method, path, headers=headers, **kwargs
        )

        if response.status_code == 401:
            self.token = None
            await self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            response = await self.client.request(
                method, path, headers=headers, **kwargs
            )

        if response.is_error:
        log.error(
            "Backend API error %s on %s: %s",
            response.status_code,
            path,
            response.text[:1000],
    )
response.raise_for_status()

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError:
            return response.text

    async def get_batch_lectures(self, batch_id: str) -> list[dict]:
        result = await self.request(
            "GET", f"/api/lectures/batch/{batch_id}"
        )

        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            for key in ("data", "content", "lectures", "items"):
                if isinstance(result.get(key), list):
                    return result[key]

        raise RuntimeError(
            "Unexpected response from GET /api/lectures/batch/{batchId}"
        )

    async def create_lecture(
        self,
        batch_id: str,
        lecture_order: int,
        title: str,
    ) -> dict:
        payload = {
            "batchId": batch_id,
            "lectureOrder": lecture_order,
            "title": title,
            "published": PUBLISHED_ON_CREATE,
        }

        result = await self.request(
            "POST", "/api/lectures", json=payload
        )

        if not isinstance(result, dict) or result.get("id") is None:
            raise RuntimeError(f"Create lecture returned no id: {result}")

        return result

    async def publish_lecture(self, lecture_id: str):
        return await self.request(
            "PATCH", f"/api/lectures/{lecture_id}/publish"
        )

    async def create_video_resource(
        self,
        lecture_id: str,
        name: str,
        file_url: str,
    ):
        return await self.request(
            "POST",
            f"/api/lecture-resources/lecture/{lecture_id}/drive-stream",
            params={"name": name, "fileUrl": file_url},
        )

    async def create_drive_resource(
        self,
        lecture_id: str,
        name: str,
        resource_type_value: str,
        file_url: str,
    ):
        return await self.request(
            "POST",
            f"/api/lecture-resources/lecture/{lecture_id}/drive",
            params={
                "name": name,
                "type": resource_type_value,
                "fileUrl": file_url,
            },
        )


# ---------------------------------------------------------------------------
# Synchronization: create missing lectures/resources only
# ---------------------------------------------------------------------------

sync_lock = asyncio.Lock()


def response_resources(lecture: dict) -> list:
    resources = lecture.get("resources")
    return resources if isinstance(resources, list) else []


async def synchronize(
    batch_id: str,
    root_folder_id: str,
    api: BackendAPI,
) -> dict:
    async with sync_lock:
        service = drive_client()

        root = await asyncio.to_thread(
            get_drive_file, service, root_folder_id
        )

        if root.get("mimeType") != FOLDER_MIME:
            raise ValueError(
                "The provided URL does not point to a Google Drive folder."
            )

        child_items = await asyncio.to_thread(
            list_children, service, root_folder_id
        )

        lecture_folders = [
            item for item in child_items
            if item.get("mimeType") == FOLDER_MIME
            and not item.get("trashed")
        ]

        lecture_folders.sort(
            key=lambda item: (item.get("name") or "").casefold()
        )

        backend_lectures = await api.get_batch_lectures(batch_id)
        backend_by_title = {}

        for lecture in backend_lectures:
            title = str(lecture.get("title") or "").strip().casefold()
            if title:
                backend_by_title.setdefault(title, lecture)

        stats = {
            "root_name": root.get("name", root_folder_id),
            "folders_found": len(lecture_folders),
            "lectures_created": 0,
            "lectures_published": 0,
            "resources_created": 0,
            "resources_skipped": 0,
            "errors": [],
        }

        for order, folder in enumerate(lecture_folders, start=1):
            folder_id = folder["id"]
            title = (folder.get("name") or f"Lecture {order}").strip()

            try:
                lecture_id = get_saved_lecture_id(batch_id, folder_id)
                backend_lecture = None

                if lecture_id:
                    backend_lecture = next(
                        (
                            item for item in backend_lectures
                            if str(item.get("id")) == str(lecture_id)
                        ),
                        None,
                    )

                    if not backend_lecture:
                        lecture_id = None

                if not lecture_id:
                    backend_lecture = backend_by_title.get(title.casefold())

                    if backend_lecture:
                        lecture_id = str(backend_lecture["id"])
                    else:
                        created = await api.create_lecture(
                            batch_id, order, title
                        )

                        backend_lecture = created
                        lecture_id = str(created["id"])
                        stats["lectures_created"] += 1

                        backend_lectures.append(created)
                        backend_by_title.setdefault(
                            title.casefold(), created
                        )

                        if PUBLISHED_ON_CREATE:
                            await api.publish_lecture(lecture_id)
                            stats["lectures_published"] += 1
                            log.info("Published lecture %s", lecture_id)

                    save_lecture(
                        batch_id, folder_id, lecture_id, title
                    )

                files = await asyncio.to_thread(
                    list_children, service, folder_id
                )

                for file_info in files:
                    if (
                        file_info.get("trashed")
                        or file_info.get("mimeType") == FOLDER_MIME
                    ):
                        continue

                    drive_file_id = file_info.get("id")
                    file_name = (
                        file_info.get("name")
                        or drive_file_id
                        or "Drive file"
                    )

                    if not drive_file_id:
                        stats["resources_skipped"] += 1
                        continue

                    if resource_already_saved(
                        batch_id, folder_id, drive_file_id
                    ):
                        stats["resources_skipped"] += 1
                        continue

                    file_url = make_drive_url(file_info)
                    kind = resource_type(file_info)

                    try:
                        if kind == "VIDEO":
                            created_resource = await api.create_video_resource(
                                lecture_id, file_name, file_url
                            )
                        else:
                            created_resource = await api.create_drive_resource(
                                lecture_id, file_name, kind, file_url
                            )

                        resource_id = (
                            created_resource.get("id")
                            if isinstance(created_resource, dict)
                            else None
                        )

                        save_resource(
                            batch_id,
                            folder_id,
                            drive_file_id,
                            resource_id,
                            file_name,
                        )

                        stats["resources_created"] += 1

                    except Exception as exc:
                        message = f"{title} / {file_name}: {exc}"
                        log.exception(
                            "Failed to create resource: %s", message
                        )
                        stats["errors"].append(message)

            except Exception as exc:
                message = f"{title}: {exc}"
                log.exception(
                    "Failed to synchronize lecture folder: %s", message
                )
                stats["errors"].append(message)

        return stats


def format_stats(stats: dict) -> str:
    lines = [
        f"Drive folder: {stats.get('root_name', '-')}",
        f"Lecture folders: {stats.get('folders_found', 0)}",
        f"New lectures: {stats.get('lectures_created', 0)}",
        f"Published lectures: {stats.get('lectures_published', 0)}",
        f"New files: {stats.get('resources_created', 0)}",
        f"Skipped files: {stats.get('resources_skipped', 0)}",
    ]

    errors = stats.get("errors") or []

    if errors:
        lines.append(f"Errors: {len(errors)}")
        lines.extend(f"- {error[:300]}" for error in errors[:8])

        if len(errors) > 8:
            lines.append(f"... and {len(errors) - 8} more errors")
    else:
        lines.append("Status: OK")

    return "\n".join(lines)


async def perform_sync(
    batch_id: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    subscription = get_subscription(batch_id)

    if not subscription:
        raise ValueError(
            "No registered batch was found. Use /sync first."
        )

    api: BackendAPI = context.application.bot_data["api"]

    try:
        stats = await synchronize(
            batch_id, subscription["folder_id"], api
        )

        if stats["errors"]:
            set_subscription_status(
                batch_id,
                "partial",
                "\n".join(stats["errors"][:10]),
            )
        else:
            set_subscription_status(batch_id, "ok", None)

        return stats

    except Exception as exc:
        set_subscription_status(batch_id, "error", str(exc))
        raise


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await reply(update, "You are not authorized to use this bot.")
        return

    await reply(
        update,
        "Welcome! This bot creates new lectures and files from Google Drive.\n\n"
        "Commands:\n"
        "/sync <folder_url> <Batch_ID> — Register the folder and start synchronization\n"
        "/syncnow <Batch_ID> — Run synchronization immediately\n"
        "/status — Show registered folders and their status\n"
        "/stop <Batch_ID> — Stop automatic synchronization",
    )


async def sync_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await reply(update, "You are not authorized to use this bot.")
        return

    if len(context.args) != 2:
        await reply(
            update,
            "Usage:\n/sync <Google_Drive_folder_URL> <Batch_ID>",
        )
        return

    folder_input, batch_id = context.args[0], context.args[1]

    try:
        folder_id = extract_drive_id(folder_input)
        service = drive_client()

        folder = await asyncio.to_thread(
            get_drive_file, service, folder_id
        )

        if folder.get("mimeType") != FOLDER_MIME:
            raise ValueError(
                "The URL does not point to a Google Drive folder."
            )

        save_subscription(batch_id, folder_id, folder_input)

        await reply(
            update,
            f"Folder '{folder.get('name', folder_id)}' registered. "
            "Synchronization started...",
        )

        stats = await perform_sync(batch_id, context)
        await reply(update, format_stats(stats))

    except Exception as exc:
        log.exception("Sync command failed")
        await reply(update, f"Synchronization failed: {exc}")


async def syncnow_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await reply(update, "You are not authorized to use this bot.")
        return

    if len(context.args) != 1:
        await reply(update, "Usage: /syncnow <Batch_ID>")
        return

    batch_id = context.args[0]

    if not get_subscription(batch_id):
        await reply(
            update,
            "No registered batch was found. Use /sync first.",
        )
        return

    await reply(update, "Synchronization started...")

    try:
        stats = await perform_sync(batch_id, context)
        await reply(update, format_stats(stats))

    except Exception as exc:
        log.exception("Sync-now command failed")
        await reply(update, f"Synchronization failed: {exc}")


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await reply(update, "You are not authorized to use this bot.")
        return

    rows = active_subscriptions()

    if not rows:
        await reply(
            update,
            "There are no active synchronization folders.",
        )
        return

    lines = ["Active folders:"]

    for row in rows:
        lines.append(
            f"\nBatch: {row['batch_id']}\n"
            f"Folder ID: {row['folder_id']}\n"
            f"Last sync: {row['last_sync_at'] or 'Not yet synchronized'}\n"
            f"Status: {row['last_status'] or 'Not synchronized yet'}"
        )

        if row["last_error"]:
            lines.append(f"Last error: {row['last_error'][:250]}")

    await reply(update, "\n".join(lines))


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        await reply(update, "You are not authorized to use this bot.")
        return

    if len(context.args) != 1:
        await reply(update, "Usage: /stop <Batch_ID>")
        return

    if stop_subscription(context.args[0]):
        await reply(
            update,
            "Automatic synchronization stopped. "
            "You can register it again using /sync.",
        )
    else:
        await reply(update, "No batch with that ID was found.")


# ---------------------------------------------------------------------------
# Background polling and lifecycle
# ---------------------------------------------------------------------------

async def polling_job(context: ContextTypes.DEFAULT_TYPE):
    api: BackendAPI = context.application.bot_data["api"]

    for subscription in active_subscriptions():
        batch_id = subscription["batch_id"]

        try:
            stats = await synchronize(
                batch_id, subscription["folder_id"], api
            )

            if stats["errors"]:
                set_subscription_status(
                    batch_id,
                    "partial",
                    "\n".join(stats["errors"][:10]),
                )
            else:
                set_subscription_status(batch_id, "ok", None)

            if (
                stats["lectures_created"]
                or stats["lectures_published"]
                or stats["resources_created"]
                or stats["errors"]
            ):
                message = (
                    f"Synchronization update for Batch {batch_id}\n"
                    + format_stats(stats)
                )

                for chat_id in TELEGRAM_ALLOWED_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=int(chat_id),
                            text=message[:4000],
                        )
                    except Exception:
                        log.exception(
                            "Could not send polling notification "
                            "to Telegram ID %s",
                            chat_id,
                        )

        except Exception as exc:
            log.exception(
                "Polling failed for batch %s", batch_id
            )

            set_subscription_status(
                batch_id, "error", str(exc)
            )

            for chat_id in TELEGRAM_ALLOWED_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            "Automatic synchronization failed "
                            f"for Batch {batch_id}: {exc}"
                        )[:4000],
                    )
                except Exception:
                    log.exception("Could not send error notification")


async def post_init(application: Application):
    init_db()

    api = BackendAPI()
    application.bot_data["api"] = api

    application.job_queue.run_repeating(
        polling_job,
        interval=POLL_INTERVAL_SECONDS,
        first=POLL_INTERVAL_SECONDS,
        name="drive-folder-polling",
    )

    log.info(
        "Bot started; polling interval is %s seconds",
        POLL_INTERVAL_SECONDS,
    )


async def post_shutdown(application: Application):
    api = application.bot_data.get("api")

    if api:
        await api.close()


def main():
    ensure_config()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("syncnow", syncnow_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()