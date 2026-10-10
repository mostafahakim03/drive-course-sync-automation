# Drive Course Sync Telegram Bot

## Features
- `/sync <Google Drive folder URL> <Batch UUID>`: register and sync immediately.
- `/status`: list active and stopped sync jobs, last sync time, and result.
- `/syncnow <Batch UUID>`: run an immediate sync.
- `/stop <Batch UUID>`: stop scheduled sync.
- Polling every `POLL_INTERVAL_SECONDS` (default 1800 seconds / 30 minutes).
- Immediate child folders of the root folder become lectures.
- Videos use `DRIVE_STREAM`; PDFs and other files use `DRIVE`.
- SQLite ledger prevents duplicate resource creation across scans.
- Only configured Telegram numeric user IDs can operate the bot.
- Admin API login uses `POST /auth/login`, validates `role == ADMIN`, and sends `Authorization: Bearer <token>`.
- New lectures are unpublished by default.

## Google Drive: private and link-public folders
The same bot can work with both:
1. **Private folder:** share the root folder with the service account's `client_email` as Viewer.
2. **Anyone-with-the-link folder:** make sure the root folder is set to Viewer for anyone with the link. The Drive API can require a resource key for link-shared files; the bot preserves `resourceKey` when the API returns it.

If a public-by-link folder still returns 403/404 through the API, share it with the service account as Viewer too. This is the most reliable method for the API to list all child folders/files. Do not change the folder to public just to make the bot work.

Important: the backend's `DRIVE_STREAM` service also validates Google Drive access using its own Google identity/configuration. For private folders, ensure the Google identity used by Spring Boot has access as well, or use the same appropriate identity/configuration. The bot's service account access alone does not automatically grant access to the backend.

## Setup
1. Create a Telegram bot using BotFather.
2. Create a Google Cloud project, enable Google Drive API, create a service account, download its JSON key as `service-account.json`.
3. Share private course root folders with the service account email as Viewer.
4. Create `.env` from `.env.example` and fill the bot token, your Telegram numeric user ID(s), and backend admin login credentials. Keep the real `.env` file out of Git.
5. Install Python 3.11+ and dependencies:
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # Linux/macOS: source .venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```

## Commands
```text
/sync https://drive.google.com/drive/folders/FOLDER_ID BATCH-UUID
/status
/syncnow BATCH-UUID
/stop BATCH-UUID
```

## Backend endpoint assumptions
- `POST /auth/login` with JSON `email`, `password`, returns `role`, `token`.
- `GET /api/lectures/batch/{batchId}` returns a list with `id`, `title`, `lectureOrder`.
- `POST /api/lectures` accepts `batchId`, `lectureOrder`, `title`, `published`.
- `POST /api/lecture-resources/lecture/{lectureId}/drive-stream` accepts query params `name`, `fileUrl`.
- `POST /api/lecture-resources/lecture/{lectureId}/drive` accepts query params `name`, `type`, `fileUrl`.

Verify query parameter names and `ResourceType` enum constants against your actual controllers/service before production. The included type map is a placeholder, not a claim about your enum values.

## Folder layout
```text
Course root/
  LEC01 - Intro/
    video.mp4
    notes.pdf
  LEC02 - Advanced/
    recording.mp4
    slides.pdf
```
Only files directly inside each lecture folder are processed; nested folders are ignored for now. Files directly inside the course root are ignored.

## Production notes
- Persist and back up `sync_state.sqlite3`; it is the duplicate-prevention ledger.
- If that database is deleted, the bot cannot reliably match existing stream resources to original Drive IDs if the backend hides their source URLs.
- First-time lecture matching uses exact title. Avoid duplicate lecture folder names in a course.
- This implementation adds new files but does not propagate Drive deletions/moves to the website.
- Store `.env` and `service-account.json` securely; never commit them.
- Consider a dedicated backend integration account with minimal permissions instead of a personal admin account for long-running production use.


## VPS deployment notes
- Set `POLL_INTERVAL_SECONDS=1800` for 30-minute polling.
- Keep the SQLite database at `/opt/course-sync-bot/data/sync_state.sqlite3` and ensure the bot's Linux user can write to that directory.
- `/status` shows active and stopped subscriptions plus the last scan time/result. `/stop` disables future scheduled scans and does not delete website content or local history. A scan already in progress may finish.
- Do not commit `.env`, service-account JSON keys, or SQLite database files.


## Before making the repository public
- Commit `.env.example`, never `.env`.
- Never commit the Google service-account JSON key, database files, access tokens, passwords, or real Telegram IDs in examples.
- Review the full Git history before publishing; `.gitignore` does not remove secrets already committed. If a credential was ever committed, rotate/revoke it.
- The example values are placeholders. Configure real values only on the VPS in a protected `.env` file.
