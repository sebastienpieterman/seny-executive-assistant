"""
FastAPI application entry point for Seny.

Provides REST API endpoints for the Seny personal assistant.
"""

import sys
print("=" * 60, flush=True)
print("SENY STARTUP: Beginning module imports...", flush=True)
print(f"SENY STARTUP: Python version: {sys.version}", flush=True)

import asyncio
print("SENY STARTUP: Imported asyncio", flush=True)

import os
print("SENY STARTUP: Imported os", flush=True)

from pathlib import Path
print("SENY STARTUP: Imported pathlib", flush=True)

from fastapi import FastAPI, Request
print("SENY STARTUP: Imported FastAPI", flush=True)

from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
print("SENY STARTUP: Imported FastAPI middleware", flush=True)

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from web.core.rate_limit import limiter
print("SENY STARTUP: Imported slowapi", flush=True)

print("SENY STARTUP: Importing routers...", flush=True)
from web.api.routes import router
print("SENY STARTUP: Imported routes", flush=True)

from web.api.auth import router as auth_router
print("SENY STARTUP: Imported auth", flush=True)

from web.api.email import router as email_router
print("SENY STARTUP: Imported email", flush=True)

from web.api.calendar import router as calendar_router
print("SENY STARTUP: Imported calendar", flush=True)

from web.api.notes import router as notes_router
print("SENY STARTUP: Imported notes", flush=True)

from web.api.tasks import router as tasks_router
print("SENY STARTUP: Imported tasks", flush=True)

from web.api.slack import router as slack_router
print("SENY STARTUP: Imported slack", flush=True)

from web.api.telegram import router as telegram_router
print("SENY STARTUP: Imported telegram", flush=True)

from web.api.sync import router as sync_router
print("SENY STARTUP: Imported sync", flush=True)

from web.api.history import router as history_router
print("SENY STARTUP: Imported history", flush=True)

from web.api.files import router as files_router
print("SENY STARTUP: Imported files", flush=True)

from web.api.upload import router as upload_router
print("SENY STARTUP: Imported upload", flush=True)

from web.api.settings import router as settings_router
print("SENY STARTUP: Imported settings", flush=True)

from web.api.location import router as location_router
print("SENY STARTUP: Imported location", flush=True)

from web.api.drive import router as drive_router
print("SENY STARTUP: Imported drive", flush=True)

from web.api.contacts import router as contacts_router
print("SENY STARTUP: Imported contacts", flush=True)

from web.api.youtube import router as youtube_router
print("SENY STARTUP: Imported youtube", flush=True)

from web.api.microsoft import router as microsoft_router
print("SENY STARTUP: Imported microsoft", flush=True)

from web.api.notifications import router as notifications_router
print("SENY STARTUP: Imported notifications", flush=True)

from web.api.second_brain import router as second_brain_router
print("SENY STARTUP: Imported second_brain", flush=True)

from web.api.dashboard import router as dashboard_router
print("SENY STARTUP: Imported dashboard", flush=True)

from web.api.scanner import router as scanner_router
print("SENY STARTUP: Imported scanner", flush=True)

from web.api.inbound import router as inbound_router
print("SENY STARTUP: Imported inbound", flush=True)

from web.api.nudges import router as nudges_router
print("SENY STARTUP: Imported nudges", flush=True)

from web.api.feedback import router as feedback_router
print("SENY STARTUP: Imported feedback", flush=True)

from web.api.voice import router as voice_router
print("SENY STARTUP: Imported voice", flush=True)

from web.api.activity import router as activity_router
print("SENY STARTUP: Imported activity", flush=True)

from web.api.telegram_webhook import router as telegram_webhook_router
print("SENY STARTUP: Imported telegram_webhook", flush=True)

from web.api.slack_events import router as slack_events_router
print("SENY STARTUP: Imported slack_events", flush=True)

from web.api.health import router as health_router
print("SENY STARTUP: Imported health", flush=True)

from web.api.embeddings import router as embeddings_router
print("SENY STARTUP: Imported embeddings", flush=True)

from web.api.search import router as search_router
print("SENY STARTUP: Imported search", flush=True)

from web.api.screen import router as screen_router
print("SENY STARTUP: Imported screen", flush=True)

from web.api.pending_actions import router as pending_actions_router
print("SENY STARTUP: Imported pending_actions", flush=True)

from web.api.lcd import router as lcd_router
print("SENY STARTUP: Imported lcd", flush=True)

from web.api.research import router as research_router
print("SENY STARTUP: Imported research", flush=True)

from web.api.qa import router as qa_router
print("SENY STARTUP: Imported qa", flush=True)

from web.core.database import init_db, list_all_google_accounts
print("SENY STARTUP: Imported database", flush=True)

from web.core.scheduler import start_scheduler, stop_scheduler
print("SENY STARTUP: Imported scheduler", flush=True)

from web.services.drive_service import DriveService
print("SENY STARTUP: Imported drive_service", flush=True)

from web.services.embedding_service import EmbeddingService, get_embedding_service
print("SENY STARTUP: Imported embedding_service", flush=True)
print("SENY STARTUP: All imports complete!", flush=True)
print("=" * 60, flush=True)

# Initialize FastAPI application
app = FastAPI(
    title="Seny API",
    description="Personal assistant API powered by Claude",
    version="1.0.0"
)


# Background task reference (to prevent garbage collection)
_background_tasks = set()

# Drive sync interval: 12 hours in seconds
DRIVE_SYNC_INTERVAL = 12 * 60 * 60

# Process-wide singleton — all code must use get_embedding_service() to avoid
# opening multiple PersistentClient instances on the same ChromaDB path.
embedding_service = get_embedding_service()


async def run_embedding_migration_for_all_users():
    """
    Background task that embeds all historical data for every user.

    Waits 30 seconds after startup to let the app fully initialize,
    then iterates all users and runs run_embedding_migration() for each.
    """
    await asyncio.sleep(30)

    try:
        from web.core.database import get_all_users_for_embedding
        users = get_all_users_for_embedding()
        print(f"[EMBEDDING] Starting historical migration for {len(users)} user(s)...", flush=True)

        for user in users:
            user_id = user["id"]
            try:
                # Self-healing: if ChromaDB is empty but tracking records exist,
                # the tracking table is stale (e.g. CHROMA_PATH changed, volume wiped).
                # Clear tracking so migration re-embeds everything from scratch.
                chroma_total = sum(
                    embedding_service.get_collection_count(et, user_id)
                    for et in ["items", "notes", "conversations", "people", "projects", "ideas"]
                )
                if chroma_total == 0:
                    from web.core.database import get_db
                    with get_db() as db:
                        _cur = db.cursor()
                        _cur.execute("SELECT COUNT(*) FROM embedding_tracking WHERE user_id = %s", (user_id,))
                        tracking_count = _cur.fetchone()[0]
                    if tracking_count > 0:
                        print(
                            f"[EMBEDDING] user_id={user_id} — ChromaDB empty but {tracking_count} stale "
                            f"tracking records found. Clearing tracking table for re-migration.",
                            flush=True,
                        )
                        from web.core.database import get_db
                        with get_db() as db:
                            db.cursor().execute("DELETE FROM embedding_tracking WHERE user_id = %s", (user_id,))
                            db.commit()

                totals = await embedding_service.run_embedding_migration(user_id)
                print(
                    f"[EMBEDDING] user_id={user_id} — "
                    f"items={totals['items']} notes={totals['notes']} "
                    f"conversations={totals['conversations']} people={totals['people']} "
                    f"projects={totals['projects']} ideas={totals['ideas']} "
                    f"total={totals['total']}",
                    flush=True,
                )
            except Exception as e:
                print(f"[EMBEDDING] Migration failed for user_id={user_id}: {repr(e)}", flush=True)

        print("[EMBEDDING] Historical migration complete.", flush=True)
    except Exception as e:
        print(f"[EMBEDDING] Migration error: {repr(e)}", flush=True)


def _scheduled_sync_blocking(user_id: int, email: str) -> dict:
    """
    Synchronous blocking function for scheduled Drive sync.
    Runs in thread pool to avoid blocking the event loop.
    """
    import asyncio as aio

    loop = aio.new_event_loop()
    aio.set_event_loop(loop)

    try:
        drive = DriveService(user_id, email)
        # Incremental sync (full_sync=False) - only fetches changes since last sync
        result = loop.run_until_complete(drive.sync_files(full_sync=False))
        return result
    finally:
        loop.close()


async def background_drive_sync():
    """
    Background task that syncs Google Drive every 12 hours.

    Runs continuously, syncing all connected Google accounts.
    Uses thread pool to avoid blocking the event loop (same as manual sync).
    """
    # Wait 5 minutes after startup before first sync
    # (let the app fully initialize and avoid slowing down startup)
    await asyncio.sleep(300)

    while True:
        try:
            print("🔄 Starting scheduled Drive sync for all accounts...", flush=True)
            accounts = list_all_google_accounts()

            if not accounts:
                print("📁 No Google accounts to sync", flush=True)
            else:
                for account in accounts:
                    try:
                        drive = DriveService(account["user_id"], account["email"])
                        if drive.is_connected():
                            # Run in thread pool to avoid blocking event loop
                            result = await asyncio.to_thread(
                                _scheduled_sync_blocking,
                                account["user_id"],
                                account["email"]
                            )
                            if "error" in result:
                                print(f"⚠️ Drive sync error for {account['email']}: {result['error']}", flush=True)
                            else:
                                print(f"✓ Drive sync complete for {account['email']}: {result.get('files_synced', 0)} files", flush=True)
                    except Exception as e:
                        print(f"⚠️ Drive sync failed for {account['email']}: {e}", flush=True)

            print(f"✓ Scheduled Drive sync complete. Next sync in 12 hours.", flush=True)

        except Exception as e:
            print(f"⚠️ Background Drive sync error: {e}", flush=True)

        # Wait 12 hours before next sync
        await asyncio.sleep(DRIVE_SYNC_INTERVAL)


@app.on_event("startup")
async def startup_event():
    """Initialize database on application startup."""
    init_db()
    print(f"✓ Seny API starting up")
    print(f"✓ Environment: {'production' if os.getenv('RAILWAY_ENVIRONMENT') else 'development'}")

    # Start notification scheduler (APScheduler - 30 second interval)
    start_scheduler()

    # Start Slack drip scanner (replaces APScheduler batch scanner for Slack)
    from web.services.slack_drip_service import start_drip_loop
    await start_drip_loop()

    # Start background Drive sync task
    task = asyncio.create_task(background_drive_sync())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    print(f"✓ Background Drive sync scheduled (every 12 hours)")

    # Start embedding migration for historical data
    if os.getenv("VOYAGE_API_KEY"):
        task = asyncio.create_task(run_embedding_migration_for_all_users())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        print("✓ Embedding migration scheduled (runs in background)")
    else:
        print("⚠️  VOYAGE_API_KEY not set — embedding migration skipped")

    # Log Slack mode (Events API vs polling)
    if os.getenv("SLACK_SIGNING_SECRET"):
        print("✓ Slack Events API configured - using webhooks (no polling)")
    else:
        print("✓ Slack using polling mode (no SLACK_SIGNING_SECRET)")

    # Configure Telegram webhook if env vars are set
    telegram_webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    app_url = os.getenv("APP_URL")

    if telegram_webhook_secret and app_url:
        # Webhook mode - configure with Telegram
        from web.services.telegram_bot_service import TelegramBotService
        bot = TelegramBotService()
        if bot.is_configured():
            webhook_url = f"{app_url.rstrip('/')}/api/webhooks/telegram"
            success = await bot.set_webhook(webhook_url, telegram_webhook_secret)
            if success:
                print(f"✓ Telegram webhook configured for {webhook_url}")
            else:
                print(f"⚠️ Failed to configure Telegram webhook")
        else:
            print(f"⚠️ Telegram webhook skipped (TELEGRAM_BOT_TOKEN not set)")
    else:
        # Polling mode (local dev / fallback)
        print(f"✓ Telegram using polling mode (no TELEGRAM_WEBHOOK_SECRET or APP_URL)")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on application shutdown."""
    stop_scheduler()
    # Stop Slack drip scanner
    from web.services.slack_drip_service import stop_drip_loop
    await stop_drip_loop()
    print("✓ Seny API shutting down")

# Configure CORS
# In production, allow Railway domain and any custom domains
# In development, allow localhost
cors_origins = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []

# Default development origins
default_origins = [
    "http://localhost:3000",  # React development server
    "http://localhost:5173",  # Vite dev server
    "http://localhost:8000",  # FastAPI development server
]

# Combine origins
allowed_origins = cors_origins + default_origins if cors_origins else default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (slowapi)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Include API routes
app.include_router(router, prefix="/api")
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(email_router, prefix="/api/email", tags=["email"])
app.include_router(calendar_router, prefix="/api/calendar", tags=["calendar"])
app.include_router(notes_router, prefix="/api/notes", tags=["notes"])
app.include_router(tasks_router, prefix="/api/tasks", tags=["tasks"])
app.include_router(slack_router, prefix="/api/slack", tags=["slack"])
app.include_router(telegram_router, prefix="/api/telegram", tags=["telegram"])
app.include_router(sync_router, prefix="/api/sync", tags=["sync"])
app.include_router(history_router, prefix="/api/history", tags=["history"])
app.include_router(files_router, prefix="/api/files", tags=["files"])
app.include_router(upload_router, prefix="/api/upload", tags=["upload"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(location_router, prefix="/api/location", tags=["location"])
app.include_router(drive_router, prefix="/api/drive", tags=["drive"])
app.include_router(contacts_router, prefix="/api/contacts", tags=["contacts"])
app.include_router(youtube_router, prefix="/api/youtube", tags=["youtube"])
app.include_router(microsoft_router, prefix="/api/microsoft", tags=["microsoft"])
app.include_router(notifications_router, prefix="/api/notifications", tags=["notifications"])
app.include_router(second_brain_router, prefix="/api/second-brain", tags=["second-brain"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(scanner_router, prefix="/api/scanner", tags=["scanner"])
app.include_router(inbound_router, prefix="/api/inbound", tags=["inbound"])
app.include_router(nudges_router, prefix="/api/nudges", tags=["nudges"])
app.include_router(feedback_router, prefix="/api/feedback", tags=["feedback"])
app.include_router(voice_router, prefix="/api/voice", tags=["voice"])
app.include_router(activity_router, prefix="/api/activity", tags=["activity"])
app.include_router(telegram_webhook_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(slack_events_router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(health_router, tags=["health"])
app.include_router(embeddings_router, prefix="/api/embeddings", tags=["embeddings"])
app.include_router(search_router, prefix="/api/search", tags=["search"])
app.include_router(screen_router)
app.include_router(pending_actions_router, prefix="/api/pending-actions", tags=["pending-actions"])
app.include_router(lcd_router, prefix="/api/lcd", tags=["lcd"])
app.include_router(research_router, prefix="/api/research", tags=["research"])
app.include_router(qa_router, prefix="/api/qa", tags=["qa"])



# --- Static file serving ---# --- Static file serving ---
# Directory paths
static_dir = Path(__file__).parent / "static"
react_dir = static_dir / "react"

# Mount static assets that are still needed (images, service worker)
images_dir = static_dir / "images"
if images_dir.exists():
    app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")

# Mount React build assets (JS, CSS, etc.) at /assets/
react_assets_dir = react_dir / "assets"
if react_assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(react_assets_dir)), name="react-assets")


@app.get("/{full_path:path}")
async def serve_react_app(request: Request, full_path: str):
    """
    Catch-all route: serve React SPA index.html for client-side routing.

    All non-API paths serve the React app's index.html so React Router
    handles routing. Legacy vanilla pages have been removed.
    """
    # Serve sw.js from static root if requested
    if full_path == "sw.js":
        sw_path = static_dir / "sw.js"
        if sw_path.is_file():
            return FileResponse(sw_path)

    # If the path points to a static file in the React build, serve it
    file_path = react_dir / full_path
    if full_path and file_path.is_file():
        return FileResponse(file_path)

    # Otherwise serve index.html for React Router
    index_path = react_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)

    return {"error": "No frontend build found. Run 'cd web/frontend && npm run build' first."}
