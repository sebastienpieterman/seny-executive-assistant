"""
Background Scheduler for Seny - Phase 7 (07-09) + Phase 8 (08-07, 08-08) + Phase 13 (Scanner Engine)

Uses APScheduler to process scheduled notifications, daily digests, weekly reviews,
and scanner jobs:
- Checks for due notifications every 30 seconds
- Sends push notifications via NotificationService
- Handles repeating alarms
- Processes task reminders
- Processes daily digests hourly (checks user timezone preferences)
- Processes weekly reviews hourly (checks user day/time preferences)
- Runs scanner jobs on staggered intervals per source
- Runs entity resolution every 30 minutes
- Runs full scan sweep every 4 hours

Usage:
    # In main.py startup event:
    from web.core.scheduler import start_scheduler, stop_scheduler
    start_scheduler()

    # In shutdown:
    stop_scheduler()
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from web.core.database import get_db, get_users_for_digest, get_users_for_weekly_review

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: Optional[AsyncIOScheduler] = None
_STARTUP_TIME: Optional[datetime] = None


def _build_nudge_sequence(
    event_id: str,
    event_title: str,
    event_start: str,
    event_end: Optional[str],
    is_all_day: bool,
    user_timezone: str,
    day_start_hour: int = 15,
) -> list:
    """
    Compute the scheduled_for timestamps for a calendar event nudge sequence.

    Timed events:  -240 min (4h), -60 min (1h), -15 min, +15 min (grace)
    All-day events: -2 days at day_start_hour, -1 day at day_start_hour, day-of at day_start_hour
    All past timestamps are skipped.

    Returns list of {offset_minutes, scheduled_for (UTC ISO string)} dicts.
    """
    tz = ZoneInfo(user_timezone)
    now_utc = datetime.now(ZoneInfo('UTC'))
    rows = []

    if is_all_day:
        try:
            event_date = datetime.strptime(event_start, "%Y-%m-%d")
        except ValueError:
            return []
        offsets = [
            (-2880, event_date - timedelta(days=2)),
            (-1440, event_date - timedelta(days=1)),
            (0,     event_date),
        ]
        for offset_min, base_date in offsets:
            fire_local = datetime(
                base_date.year, base_date.month, base_date.day,
                day_start_hour, 0, 0, tzinfo=tz
            )
            fire_utc = fire_local.astimezone(ZoneInfo('UTC'))
            if fire_utc > now_utc:
                rows.append({
                    'offset_minutes': offset_min,
                    'scheduled_for': fire_utc.strftime('%Y-%m-%dT%H:%M:%S'),
                })
    else:
        try:
            if event_start.endswith('Z'):
                event_start = event_start[:-1] + '+00:00'
            start_dt = datetime.fromisoformat(event_start)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)
            start_utc = start_dt.astimezone(ZoneInfo('UTC'))
        except ValueError:
            return []
        offsets = [-240, -60, -15, 15]
        for offset_min in offsets:
            fire_utc = start_utc + timedelta(minutes=offset_min)
            if fire_utc > now_utc:
                rows.append({
                    'offset_minutes': offset_min,
                    'scheduled_for': fire_utc.strftime('%Y-%m-%dT%H:%M:%S'),
                })

    return rows


async def sync_upcoming_calendar_nudges():
    """
    Daily job: scan all upcoming calendar events for all users and schedule
    nudge sequences for any event that doesn't already have one.

    Handles both Google Calendar and Outlook Calendar connections.
    Skips events in the past. Idempotent — has_event_nudge_sequence() prevents duplicates.
    """
    from web.core.database import (
        get_db, has_event_nudge_sequence, schedule_event_nudge_sequence,
        get_nudge_preferences
    )
    from web.services.calendar_service import CalendarService
    from web.services.outlook_calendar_service import OutlookCalendarService
    import json

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()
    except Exception as e:
        logger.error("sync_upcoming_calendar_nudges: DB error fetching users: %s", repr(e))
        return

    for row in users:
        user_id = row['id']
        try:
            # Get user timezone and day_start_hour from user_settings
            with get_db() as db:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT digest_timezone, day_start_hour FROM user_settings WHERE user_id = %s",
                    (user_id,)
                )

                settings_row = cursor.fetchone()
            user_tz = (settings_row['digest_timezone'] if settings_row else None) or 'America/Chicago'
            day_start = (settings_row['day_start_hour'] if settings_row else None) or 15

            # --- Google Calendar ---
            try:
                google_accounts = CalendarService.list_connected_accounts(user_id)
                for account in google_accounts:
                    email = account['email']
                    cal = CalendarService(user_id, email)
                    events = await cal.get_events(days_ahead=14, timezone=user_tz)
                    for event in events:
                        event_id = event.get('id', '')
                        if not event_id or has_event_nudge_sequence(user_id, event_id):
                            continue
                        # Detect all-day: Google returns {'date': ...} vs {'dateTime': ...}
                        start_info = event.get('start', {})
                        is_all_day = 'date' in start_info and 'dateTime' not in start_info
                        event_start = start_info.get('dateTime') or start_info.get('date', '')
                        event_end_info = event.get('end', {})
                        event_end = event_end_info.get('dateTime') or event_end_info.get('date')
                        attendees = event.get('attendees', [])
                        attendees_json = json.dumps([a.get('email', '') for a in attendees]) if attendees else None
                        description = event.get('description') or event.get('summary', '')
                        title = event.get('summary', 'Calendar Event')

                        nudge_rows = _build_nudge_sequence(
                            event_id, title, event_start, event_end,
                            is_all_day, user_tz, day_start
                        )
                        if nudge_rows:
                            schedule_event_nudge_sequence(
                                user_id, event_id, title, event_start, event_end,
                                is_all_day, attendees_json, description, nudge_rows
                            )
            except Exception as e:
                logger.error("sync_upcoming_calendar_nudges: Google error user=%s: %s", user_id, repr(e))

            # --- Outlook Calendar ---
            try:
                outlook_accounts = OutlookCalendarService.list_connected_accounts(user_id)
                for account in outlook_accounts:
                    email = account['email']
                    cal = OutlookCalendarService(user_id, email)
                    events = await cal.get_events(days_ahead=14)
                    for event in events:
                        event_id = event.get('id', '')
                        if not event_id or has_event_nudge_sequence(user_id, event_id):
                            continue
                        event_start = event.get('start', '')
                        # Detect all-day: date-only string (no 'T')
                        is_all_day = 'T' not in event_start and len(event_start) == 10
                        event_end = event.get('end')
                        title = event.get('subject') or event.get('summary', 'Calendar Event')
                        attendees = event.get('attendees', [])
                        attendees_json = json.dumps(attendees) if attendees else None
                        description = event.get('body') or event.get('description', '')

                        nudge_rows = _build_nudge_sequence(
                            event_id, title, event_start, event_end,
                            is_all_day, user_tz, day_start
                        )
                        if nudge_rows:
                            schedule_event_nudge_sequence(
                                user_id, event_id, title, event_start, event_end,
                                is_all_day, attendees_json, description, nudge_rows
                            )
            except Exception as e:
                logger.error("sync_upcoming_calendar_nudges: Outlook error user=%s: %s", user_id, repr(e))

        except Exception as e:
            logger.error("sync_upcoming_calendar_nudges: error for user=%s: %s", user_id, repr(e))

    logger.info("sync_upcoming_calendar_nudges: complete")


async def process_calendar_event_nudges():
    """
    Runs every 10 minutes. Fires due calendar event nudges with escalating tone.

    For each due pending row:
    1. Check if user has already acknowledged an earlier nudge in the sequence → cancel remaining
    2. If grace row (offset_minutes=15): call Claude Haiku to decide whether to send
    3. Build message based on offset_minutes
    4. Deliver via NudgeService
    5. Mark row as sent
    """
    from web.core.database import (
        get_db, get_due_event_nudges, mark_event_nudge_sent, cancel_event_nudge_sequence
    )
    from web.services.nudge_service import NudgeService

    try:
        due_rows = get_due_event_nudges()
    except Exception as e:
        logger.error("process_calendar_event_nudges: error fetching due rows: %s", repr(e))
        return

    if not due_rows:
        return

    logger.info("process_calendar_event_nudges: %d due rows to process", len(due_rows))

    for row in due_rows:
        row_id = row['id']
        user_id = row['user_id']
        event_id = row['event_id']
        event_title = row['event_title']
        offset = row['offset_minutes']
        is_all_day = bool(row['is_all_day'])
        attendees_json = row.get('event_attendees') or '[]'
        description = row.get('event_description') or ''

        try:
            # Step 1: Check for prior acknowledgement in this sequence
            acknowledged = _check_event_acknowledged(user_id, event_id)
            if acknowledged:
                cancel_event_nudge_sequence(user_id, event_id)
                logger.info("process_calendar_event_nudges: sequence acknowledged, cancelled remaining for event=%s user=%s", event_id, user_id)
                continue

            # Step 2: Grace row — ask Haiku whether to send
            if offset == 15:
                should_send = await _should_send_grace_nudge(event_title, attendees_json, description)
                if not should_send:
                    # Mark as cancelled — Haiku decided it's not appropriate
                    with get_db() as db:
                        db.cursor().execute(
                            "UPDATE calendar_event_nudges SET status='cancelled' WHERE id=%s",
                            (row_id,)
                        )
                    continue

            # Step 3: Build message
            title, body = _build_calendar_nudge_message(event_title, offset, is_all_day)

            # Step 4: Deliver
            service = NudgeService(user_id)
            result = await service.send_nudge(
                nudge_type='calendar_event',
                title=title,
                body=body,
                urgency='urgent' if offset in (-15, 15) else 'normal',
                source_type='calendar_event_nudge',
                source_id=row_id,
            )

            # Step 5: Mark sent
            if result.get('nudge_id'):
                mark_event_nudge_sent(row_id, result['nudge_id'])
            else:
                # Delivery failed — mark as cancelled so we don't retry indefinitely
                with get_db() as db:
                    db.cursor().execute(
                        "UPDATE calendar_event_nudges SET status='cancelled' WHERE id=%s",
                        (row_id,)
                    )

        except Exception as e:
            logger.error("process_calendar_event_nudges: error on row=%s event=%s: %s", row_id, event_id, repr(e))


def _check_event_acknowledged(user_id: int, event_id: str) -> bool:
    """
    Return True if any earlier sent nudge in this event's sequence was acted on.
    Uses nudges.acted_at (set by Phase 37 reply threading when user responds).
    """
    from web.core.database import get_db
    try:
        with get_db() as db:
            result = db.cursor().execute("""
                SELECT COUNT(*) FROM calendar_event_nudges cen
                JOIN nudges n ON cen.nudge_id = n.id
                WHERE cen.user_id = %s AND cen.event_id = %s
                  AND cen.status = 'sent'
                  AND n.acted_at IS NOT NULL
            """, (user_id, event_id)).fetchone()
        return result[0] > 0
    except Exception as e:
        logger.warning("_check_event_acknowledged error: %s", repr(e))
        return False


async def _should_send_grace_nudge(event_title: str, attendees_json: str, description: str) -> bool:
    """
    Ask Claude Haiku whether the grace period nudge is appropriate for this event.
    Returns True to send, False to skip. Defaults to False on any error (fail safe).
    """
    import json
    try:
        import anthropic
        attendees = json.loads(attendees_json) if attendees_json else []
        attendee_str = ', '.join(attendees[:5]) if attendees else 'none listed'
        desc_excerpt = description[:200] if description else 'no description'

        prompt = (
            f"Calendar event: '{event_title}'\n"
            f"Attendees: {attendee_str}\n"
            f"Description: {desc_excerpt}\n\n"
            f"This event started about 15 minutes ago. Should I send the user a "
            f"'you can probably still make it' nudge%s Answer YES or NO only."
        )
        client = anthropic.Anthropic()
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=10,
            messages=[{'role': 'user', 'content': prompt}]
        )
        answer = response.content[0].text.strip().upper()
        return answer.startswith('YES')
    except Exception as e:
        logger.warning("_should_send_grace_nudge Haiku call failed: %s", repr(e))
        return False  # Fail safe: don't send if unsure


def _build_calendar_nudge_message(event_title: str, offset_minutes: int, is_all_day: bool) -> tuple[str, str]:
    """
    Return (title, body) for the nudge based on how far out the event is.
    Tone escalates as the event approaches.
    """
    if is_all_day:
        if offset_minutes <= -2880:
            return f"In 2 days: {event_title}", f"Just a heads up — {event_title} is happening in 2 days."
        elif offset_minutes <= -1440:
            return f"Tomorrow: {event_title}", f"Tomorrow's the day — {event_title}. Anything you need to sort out before then?"
        else:
            return f"Today: {event_title}", f"{event_title} is today. Don't let it sneak up on you."
    else:
        if offset_minutes <= -240:
            return f"In 4 hours: {event_title}", f"Hey — {event_title} is in 4 hours. Worth knowing it's coming."
        elif offset_minutes <= -60:
            return f"1 hour: {event_title}", f"One hour out — {event_title}. Anything you need to prep or pull together?"
        elif offset_minutes <= -15:
            return f"15 min: {event_title}", f"{event_title} starts in 15 minutes."
        else:
            return f"Still time: {event_title}", f"{event_title} started about 15 minutes ago — might still be worth joining."


async def process_scheduled_notifications():
    """
    Process due scheduled notifications.

    Runs every 30 seconds to check for notifications that need to be sent.
    Handles one-time and repeating notifications.
    """
    from web.services.notification_service import NotificationService, calculate_next_occurrence

    try:
        now = datetime.utcnow()
        window = now + timedelta(seconds=35)  # Check 35-second window (with buffer)

        with get_db() as db:
            cursor = db.cursor()

            # Get notifications due in next 35 seconds
            cursor.execute("""
                SELECT id, user_id, title, body, url, type,
                       scheduled_for, timezone, repeat_pattern, repeat_until
                FROM scheduled_notifications
                WHERE status = 'pending'
                AND scheduled_for <= %s
                ORDER BY scheduled_for
            """, (window.isoformat(),))

            pending = cursor.fetchall()

            for row in pending:
                notif_id = row['id']
                user_id = row['user_id']
                title = row['title']
                body = row['body']
                url = row['url']
                notif_type = row['type']
                scheduled_for = row['scheduled_for']
                timezone = row['timezone']
                repeat_pattern = row['repeat_pattern']
                repeat_until = row['repeat_until']

                try:
                    # Send the notification
                    service = NotificationService(user_id)
                    result = await service.send_notification(
                        title=title,
                        body=body,
                        url=url,
                        notification_type=notif_type
                    )

                    # Mark as sent
                    cursor.execute("""
                        UPDATE scheduled_notifications
                        SET status = 'sent', sent_at = %s
                        WHERE id = %s
                    """, (datetime.utcnow().isoformat(), notif_id))

                    # Handle repeating notifications (alarms)
                    if repeat_pattern and notif_type == 'alarm':
                        scheduled_dt = datetime.fromisoformat(scheduled_for)
                        next_time = calculate_next_occurrence(scheduled_dt, repeat_pattern)

                        # Check if we should create next occurrence
                        should_repeat = next_time is not None
                        if repeat_until:
                            repeat_until_dt = datetime.fromisoformat(repeat_until)
                            should_repeat = should_repeat and next_time <= repeat_until_dt

                        if should_repeat:
                            # Create next occurrence
                            await service.schedule_notification(
                                title=title,
                                body=body,
                                scheduled_for=next_time,
                                notification_type='alarm',
                                url=url,
                                repeat_pattern=repeat_pattern,
                                repeat_until=datetime.fromisoformat(repeat_until) if repeat_until else None,
                                timezone=timezone
                            )
                            logger.info(f"Scheduled next alarm occurrence for {next_time}")

                    logger.info(f"Sent notification {notif_id}: {title} (sent={result['sent']}, failed={result['failed']})")

                except Exception as e:
                    logger.error(f"Failed to send notification {notif_id}: {repr(e)}")
                    cursor.execute("""
                        UPDATE scheduled_notifications
                        SET status = 'failed', error_message = %s
                        WHERE id = %s
                    """, (repr(e), notif_id))

            db.commit()

    except Exception as e:
        logger.error(f"Error in notification processor: {repr(e)}")


async def process_task_reminders():
    """
    Process due task reminders.

    Checks for task reminders that are due and sends push notifications.
    Marks reminders as sent to prevent duplicate notifications.
    """
    from web.services.notification_service import NotificationService

    try:
        now = datetime.utcnow()
        window = now + timedelta(seconds=35)  # Check 35-second window

        with get_db() as db:
            cursor = db.cursor()

            # Get task reminders that are due
            # task_reminders has: id, task_id, remind_at, reminder_type, is_sent, sent_at
            # tasks has: id, user_id, title, description, status, etc.
            cursor.execute("""
                SELECT tr.id, tr.task_id, tr.remind_at, t.user_id, t.title, t.description, t.status
                FROM task_reminders tr
                JOIN tasks t ON tr.task_id = t.id
                WHERE tr.is_sent = 0
                AND tr.remind_at <= %s
                AND t.status != 'completed'
                ORDER BY tr.remind_at
            """, (window.isoformat(),))

            due_reminders = cursor.fetchall()

            for row in due_reminders:
                reminder_id = row['id']
                task_id = row['task_id']
                remind_at = row['remind_at']
                user_id = row['user_id']
                task_title = row['title']
                task_desc = row['description']
                task_status = row['status']

                try:
                    service = NotificationService(user_id)
                    result = await service.send_notification(
                        title="📋 Task Reminder",
                        body=task_title,
                        url=f"/%stask={task_id}",
                        notification_type="task_reminder"
                    )

                    # Mark reminder as sent
                    cursor.execute("""
                        UPDATE task_reminders
                        SET is_sent = 1, sent_at = %s
                        WHERE id = %s
                    """, (datetime.utcnow().isoformat(), reminder_id))

                    logger.info(f"Sent task reminder {reminder_id} for task '{task_title}'")

                except Exception as e:
                    logger.error(f"Failed to send task reminder {reminder_id}: {repr(e)}")

            db.commit()

    except Exception as e:
        logger.error(f"Error in task reminder processor: {repr(e)}")


async def notification_job():
    """
    Combined job that processes both scheduled notifications and task reminders.

    This is the main job registered with APScheduler.
    Runs every 90 seconds. Logs duration for observability.
    """
    import time as _time
    start = _time.time()
    await process_scheduled_notifications()
    await process_task_reminders()
    duration = _time.time() - start
    logger.info(f"notification_job completed in {duration:.1f}s")
    if duration > 90:
        logger.warning(f"notification_job took {duration:.1f}s — longer than 90s interval, may cause starvation")


async def process_daily_digests():
    """
    Process daily digests for users whose delivery time matches current hour.

    Runs every hour at minute 0 to check for users who should receive
    their daily digest based on their timezone and delivery time preferences.
    """
    from web.services.digest_service import DigestService

    try:
        # Get current hour in UTC
        now = datetime.utcnow()
        current_hour = now.hour

        # Get users whose digest time matches
        users = get_users_for_digest(current_hour)

        if not users:
            logger.debug(f"No users with digest due at hour {current_hour}")
            return

        logger.info(f"Processing digests for {len(users)} users at hour {current_hour}")

        for user_prefs in users:
            user_id = user_prefs['user_id']
            try:
                digest_service = DigestService(user_id)
                result = await digest_service.deliver_digest()

                if result.get('generated'):
                    logger.info(
                        f"Delivered digest for user {user_id}: "
                        f"email={result.get('email_sent')}, push={result.get('push_sent')}"
                    )
                else:
                    logger.debug(f"Digest not generated for user {user_id}: {result.get('reason')}")

            except Exception as e:
                logger.error(f"Failed to deliver digest for user {user_id}: {repr(e)}")

        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("daily-digest")

    except Exception as e:
        logger.error(f"Error in daily digest processor: {repr(e)}")


async def process_weekly_reviews():
    """
    Process weekly reviews for users whose review day and time matches now.

    Runs every hour at minute 0 to check for users who should receive
    their weekly review based on their timezone and delivery preferences.
    """
    from web.services.digest_service import DigestService

    try:
        # Get current day and hour in UTC
        now = datetime.now(ZoneInfo('UTC'))
        current_day = now.strftime('%A').lower()  # 'sunday', 'monday', etc.
        current_hour = now.hour

        # Check for common weekly review days
        for check_day in ['sunday', 'saturday', 'friday']:
            users = get_users_for_weekly_review(check_day, current_hour)

            if not users:
                continue

            logger.info(f"Processing weekly reviews for {len(users)} users on {check_day}")

            for user_prefs in users:
                user_id = user_prefs['user_id']
                try:
                    digest_service = DigestService(user_id)
                    result = await digest_service.deliver_weekly_review()

                    if result.get('generated'):
                        logger.info(
                            f"Delivered weekly review for user {user_id}: "
                            f"email={result.get('email_sent')}, push={result.get('push_sent')}"
                        )
                    else:
                        logger.debug(f"Weekly review not generated for user {user_id}: {result.get('reason')}")

                except Exception as e:
                    logger.error(f"Failed to deliver weekly review for user {user_id}: {repr(e)}")

    except Exception as e:
        logger.error(f"Error in weekly review processor: {repr(e)}")


def _sync_drive_for_scheduler(user_id: int, email: str):
    """Blocking Drive sync for scheduler thread pool."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from web.services.drive_service import DriveService
        drive = DriveService(user_id, email)
        loop.run_until_complete(drive.sync_files(full_sync=False))
    finally:
        loop.close()


async def process_drive_sync():
    """Auto-sync Google Drive files for all users with connected accounts."""
    from web.core.database import list_gmail_tokens

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT DISTINCT user_id FROM google_tokens")

            users = cursor.fetchall()

        for (user_id,) in users:
            accounts = list_gmail_tokens(user_id)
            for account in accounts:
                email = account["email"]
                try:
                    await asyncio.to_thread(
                        _sync_drive_for_scheduler, user_id, email
                    )
                    logger.info(f"Drive auto-sync complete for {email}")
                except Exception as e:
                    logger.error(f"Drive auto-sync error for {email}: {repr(e)}")
    except Exception as e:
        logger.error(f"Error in Drive auto-sync processor: {repr(e)}")


async def process_scanner(source: str):
    """
    Run a scanner job for a specific source across all users.

    Gets all users, creates ScannerService per user, runs scan for that source.
    Catches per-user exceptions so one user failure doesn't stop others.
    Checks if the user has the relevant integration configured before scanning.
    """
    # HF-09: Slack is handled by the continuous drip loop — block any batch path.
    # The dedicated APScheduler Slack job was removed in HF-08-03. This guard
    # is defensive: prevents any future call path from triggering batch scanning.
    if source == 'slack':
        logger.info(
            "process_scanner('slack') blocked — Slack handled by drip loop (slack_drip_service.py)"
        )
        return

    from web.services.scanner_service import ScannerService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug(f"Scanner ({source}): no users found")
            return

        for row in users:
            user_id = row['id']
            try:
                service = ScannerService(user_id)
                result = await service.run_scan(source)
                if result.get('status') == 'completed':
                    logger.info(
                        f"Scanner ({source}): user={user_id} found={result.get('items_found', 0)} "
                        f"new={result.get('items_new', 0)} duration={result.get('duration_seconds', 0)}s"
                    )
                elif result.get('status') == 'skipped':
                    logger.debug(f"Scanner ({source}): user={user_id} skipped ({result.get('reason')})")
            except Exception as e:
                logger.error(f"Scanner ({source}): error for user {user_id}: {repr(e)}")

        if source in ('gmail', 'telegram'):
            from web.core.database import update_heartbeat as _update_heartbeat
            _update_heartbeat(f"scanner-{source}")

    except Exception as e:
        logger.error(f"Error in scanner processor ({source}): {repr(e)}")


async def process_entity_resolution():
    """
    Run entity resolution for all users.

    Runs every 30 minutes to match identities from scanned items to People DB entries.
    """
    from web.services.entity_resolver import EntityResolver

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            return

        for row in users:
            user_id = row['id']
            try:
                resolver = EntityResolver(user_id)
                result = await resolver.resolve_all()
                logger.info(
                    f"Entity resolution: user={user_id} new={result.get('new_mappings', 0)} "
                    f"updated={result.get('updated_mappings', 0)} unresolved={result.get('unresolved', 0)}"
                )
            except Exception as e:
                logger.error(f"Entity resolution: error for user {user_id}: {repr(e)}")

    except Exception as e:
        logger.error(f"Error in entity resolution processor: {repr(e)}")


async def process_full_scan():
    """
    Run all source scans + entity resolution for all users.

    Full sweep every 4 hours to catch anything missed by individual source jobs.
    """
    from web.services.scanner_service import ScannerService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            return

        for row in users:
            user_id = row['id']
            try:
                service = ScannerService(user_id)
                result = await service.run_all_scans(resolve_entities=True)
                completed = sum(1 for r in result.get('scan_results', []) if r.get('status') == 'completed')
                failed = sum(1 for r in result.get('scan_results', []) if r.get('status') == 'failed')
                logger.info(
                    f"Full scan: user={user_id} completed={completed} failed={failed}"
                )
            except Exception as e:
                logger.error(f"Full scan: error for user {user_id}: {repr(e)}")

    except Exception as e:
        logger.error(f"Error in full scan processor: {repr(e)}")


# Scanner source job wrappers (APScheduler needs callable, not partial)
# Note: _scan_slack removed in HF-08 — Slack now uses continuous drip loop (slack_drip_service.py)
async def _scan_gmail(): await process_scanner('gmail')
async def _scan_telegram(): await process_scanner('telegram')
async def _scan_calendar(): await process_scanner('calendar')
async def _scan_drive_scanner(): await process_scanner('drive')
async def _scan_contacts(): await process_scanner('contacts')
async def _scan_notes(): await process_scanner('notes')
async def _scan_tasks(): await process_scanner('tasks')
async def _scan_location(): await process_scanner('location')
async def _scan_conversations(): await process_scanner('conversations')


# Telegram bot polling wrapper
async def _process_telegram_bot():
    """Process Telegram bot messages - called every 5 seconds."""
    from web.services.telegram_bot_worker import process_telegram_bot_messages
    await process_telegram_bot_messages()


# Slack bot polling wrapper
async def _process_slack_bot():
    """Process Slack bot messages - called every 10 seconds."""
    from web.services.slack_bot_worker import process_slack_bot_messages
    await process_slack_bot_messages()


async def process_inbound_classification():
    """
    Process newly scanned items through the classification pipeline.

    Runs every 20 minutes. Processes one batch (50 items) per user per run.
    If there's a backlog, it drains over multiple cycles rather than blocking.
    """
    from web.services.inbound_processor import InboundProcessor

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            return

        for row in users:
            user_id = row['id']
            try:
                processor = InboundProcessor(user_id)
                result = await processor.process_batch(batch_size=500)
                if result['total'] > 0:
                    logger.info(
                        "Inbound processing for user %d: %d items "
                        "(%d classified, %d filtered, %d failed) in %.1fs",
                        user_id, result['total'], result['classified'],
                        result['filtered'], result['failed'],
                        result['duration_seconds']
                    )
            except Exception as e:
                logger.error("Inbound processing error for user %d: %r", user_id, e)

        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("inbound-classification")

    except Exception as e:
        logger.error("Inbound processing job error: %r", e)


async def process_meeting_prep():
    """Send pre-meeting briefings for events starting in 30-90 minutes."""
    from web.services.predictive_service import PredictiveService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug("Meeting prep: no users found")
            return

        total_sent = 0

        for row in users:
            user_id = row['id']
            try:
                service = PredictiveService(user_id)
                result = await service.send_meeting_prep_nudges()
                sent = result.get('sent', 0)
                total_sent += sent
                if sent > 0:
                    logger.info(
                        "Meeting prep: user=%d sent=%d briefings",
                        user_id, sent
                    )
            except Exception as e:
                logger.error("[MEETING_PREP] User %d failed: %r", user_id, e)

        if total_sent > 0:
            logger.info("Meeting prep job complete: %d briefings sent", total_sent)

    except Exception as e:
        logger.error("Meeting prep job error: %r", e)


async def process_relationship_predictions():
    """Daily check for stale relationships and open follow-ups. Phase 27-02."""
    from web.services.predictive_service import PredictiveService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug("Relationship predictions: no users found")
            return

        for row in users:
            user_id = row['id']
            try:
                service = PredictiveService(user_id)
                rel_result = await service.send_relationship_nudges()
                fu_result = await service.send_followup_nudges()
                checkin_result = await service.send_family_checkin_nudges()
                total = rel_result.get('sent', 0) + fu_result.get('sent', 0) + checkin_result.get('sent', 0)
                if total > 0:
                    logger.info(
                        "Relationship predictions: user=%d relationship=%d followup=%d checkin=%d",
                        user_id, rel_result.get('sent', 0), fu_result.get('sent', 0), checkin_result.get('sent', 0),
                    )
            except Exception as e:
                logger.error("[RELATIONSHIP_PRED] User %d failed: %r", user_id, e)

    except Exception as e:
        logger.error("Relationship predictions job error: %r", e)


async def process_nudge_followups():
    """
    Follow up on overdue_task nudges that received no response in 4–24 hours.
    Phase 33 — Nudge follow-up loop.
    """
    from web.services.predictive_service import PredictiveService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug("Nudge followups: no users found")
            return

        for row in users:
            user_id = row['id']
            try:
                service = PredictiveService(user_id)
                result = await service.send_task_followup_nudges()
                if result['sent'] > 0:
                    logger.info(
                        "Nudge followups: user=%d sent=%d",
                        user_id, result['sent'],
                    )
            except Exception as e:
                logger.error(
                    "Nudge followup job failed for user %d: %r",
                    user_id, e,
                )

    except Exception as e:
        logger.error("Nudge followup job error: %r", e)


async def process_upcoming_task_nudges():
    """
    Nudge about tasks due within the next 24–48 hours, before they go overdue.
    Phase 34 — Smart forward-looking nudges (Layer 1).
    """
    from web.services.predictive_service import PredictiveService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        for row in users:
            user_id = row['id']
            try:
                service = PredictiveService(user_id)
                result = await service.send_upcoming_task_nudges()
                if result['sent'] > 0:
                    logger.info(
                        "Upcoming task nudges: user=%d sent=%d",
                        user_id, result['sent'],
                    )
            except Exception as e:
                logger.error(
                    "Upcoming task nudge job failed for user %d: %r",
                    user_id, e,
                )

    except Exception as e:
        logger.error("Upcoming task nudge job error: %r", e)


async def process_ai_coach_nudges():
    """
    Send a smart focus-coaching nudge every 2–3 hours during waking hours.
    Phase 34 — Smart forward-looking nudges (Layer 2).
    Uses Claude Haiku to decide what the user should work on right now.
    """
    from web.services.predictive_service import PredictiveService
    from web.services.nudge_service import NudgeService

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        for row in users:
            user_id = row['id']
            try:
                # Respect quiet hours before calling Claude
                nudge_svc = NudgeService(user_id)
                if nudge_svc.is_quiet_hours():
                    logger.debug("AI coach: quiet hours for user %d, skipping", user_id)
                    continue

                service = PredictiveService(user_id)
                result = await service.send_ai_coach_nudge()
                if result['sent']:
                    logger.info("AI coach nudge sent for user %d", user_id)
            except Exception as e:
                logger.error(
                    "AI coach nudge job failed for user %d: %r",
                    user_id, e,
                )

    except Exception as e:
        logger.error("AI coach nudge job error: %r", e)


async def process_people_auto_tracker():
    """
    Auto-update People tracker from scanned communications.

    Runs every 15 minutes. For each user with tracked people:
    1. Find recent inbound communications linked to People via entity_mappings
    2. Update last_contact_date if newer than existing
    3. Use Haiku to extract noteworthy context to add to notes

    Phase 19-01 - Automatic People Tracker
    """
    from web.services.people_auto_tracker import PeopleAutoTracker

    try:
        with get_db() as db:
            cursor = db.cursor()
            # Get users who have at least one person tracked
            cursor.execute("""
                SELECT DISTINCT user_id FROM people
            """)
            users = cursor.fetchall()

        if not users:
            logger.debug("People auto-tracker: no users with tracked people")
            return

        total_updated = 0
        total_contexts = 0

        for row in users:
            user_id = row['user_id']
            try:
                tracker = PeopleAutoTracker(user_id)
                result = await tracker.run()

                total_updated += result.get('people_updated', 0)
                total_contexts += result.get('contexts_added', 0)

                if result.get('people_updated', 0) > 0:
                    logger.info(
                        "People auto-tracker for user %d: %d updated, %d contexts added",
                        user_id, result.get('people_updated', 0),
                        result.get('contexts_added', 0)
                    )

            except Exception as e:
                logger.error("People auto-tracker error for user %d: %r", user_id, e)

        if total_updated > 0 or total_contexts > 0:
            logger.info(
                "People auto-tracker job complete: %d people updated, %d contexts added",
                total_updated, total_contexts
            )
        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("people-auto-tracker")

    except Exception as e:
        logger.error("People auto-tracker job error: %r", e)


async def process_urgent_nudges():
    """
    Process urgent nudges for all users with nudge_enabled.

    Runs every 10 minutes. Queries pending detected_actions, urgent classifications,
    and overdue tasks. Routes to nudge queue respecting quiet hours, dedup, rate limits.
    """
    # Import inside function to avoid circular imports
    from web.services.nudge_service import NudgeService
    from web.core.database import get_nudge_preferences

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug("Urgent nudges: no users found")
            return

        total_queued = 0
        users_processed = 0

        for row in users:
            user_id = row['id']
            try:
                # Check if nudges are enabled for this user
                prefs = get_nudge_preferences(user_id)
                if not prefs.get('nudge_enabled', True):
                    logger.debug("Nudges disabled for user %d, skipping", user_id)
                    continue

                # Process pending nudges
                service = NudgeService(user_id)
                result = await service.process_pending_nudges()

                users_processed += 1
                total_queued += result.get('queued', 0)

                logger.debug(
                    "Urgent nudges for user %d: queued=%d skipped_dup=%d skipped_rate=%d",
                    user_id, result.get('queued', 0),
                    result.get('skipped_dup', 0), result.get('skipped_rate_limit', 0)
                )

            except Exception as e:
                logger.error("Urgent nudge processing error for user %d: %r", user_id, e)

        logger.info(
            "Urgent nudge job complete: %d users processed, %d total nudges queued",
            users_processed, total_queued
        )
        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("urgent-nudges")

    except Exception as e:
        logger.error("Urgent nudge job error: %r", e)


async def process_batch_nudges():
    """
    Process batch nudges for all users with nudge_enabled.

    Runs hourly at :30. Checks if batch interval has elapsed per user's
    nudge_batch_interval_hours and sends mini-digest of pending normal-priority nudges.
    """
    # Import inside function to avoid circular imports
    from web.services.nudge_service import NudgeService
    from web.core.database import get_nudge_preferences

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        if not users:
            logger.debug("Batch nudges: no users found")
            return

        batches_sent = 0
        users_processed = 0

        for row in users:
            user_id = row['id']
            try:
                # Check if nudges are enabled for this user
                prefs = get_nudge_preferences(user_id)
                if not prefs.get('nudge_enabled', True):
                    logger.debug("Nudges disabled for user %d, skipping batch", user_id)
                    continue

                # Send batch if due
                service = NudgeService(user_id)
                result = await service.send_batch_if_due()

                users_processed += 1
                if result.get('sent'):
                    batches_sent += 1
                    logger.debug(
                        "Batch nudge sent for user %d: %d items via %s (batch_id=%s)",
                        user_id, result.get('nudge_count', 0),
                        result.get('channel'), result.get('batch_id')
                    )

            except Exception as e:
                logger.error("Batch nudge processing error for user %d: %r", user_id, e)

        logger.info(
            "Batch nudge job complete: %d users processed, %d batches sent",
            users_processed, batches_sent
        )

    except Exception as e:
        logger.error("Batch nudge job error: %r", e)


async def process_drip_nudges():
    """
    Send one conversational check-in per user on drip interval.

    Runs every 15 minutes. Checks per-user drip interval before sending
    to avoid overwhelming users who set longer intervals.

    Phase 37 — Conversational Nudge Flow.
    """
    from web.services.nudge_service import NudgeService
    from web.core.database import get_nudge_preferences

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()

        for row in users:
            user_id = row['id']
            try:
                prefs = get_nudge_preferences(user_id)
                if not prefs.get('nudge_enabled', True):
                    continue
                service = NudgeService(user_id)
                result = await service.send_drip_if_due()
                if result.get('sent'):
                    logger.info("Drip nudge sent for user %d via %s", user_id, result.get('channel'))
            except Exception as e:
                logger.error("Drip nudge error for user %d: %r", user_id, e)
        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("drip-nudges")
    except Exception as e:
        logger.error("Drip nudge job error: %r", e)


async def compute_user_patterns():
    """
    Recompute pattern preferences for all users with recent feedback.

    Runs daily at 3:00 AM. Processes users who have submitted feedback
    in the last 7 days to keep their patterns up to date.

    Phase 17-03: User Pattern Learning
    """
    from web.services.pattern_learning_service import PatternLearningService

    try:
        with get_db() as db:
            cursor = db.cursor()
            # Get users with feedback in last 7 days
            seven_days_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
            cursor.execute("""
                SELECT DISTINCT user_id FROM user_feedback
                WHERE created_at >= %s
            """, (seven_days_ago,))
            rows = cursor.fetchall()

        user_ids = [row['user_id'] for row in rows]

        if not user_ids:
            logger.debug("Pattern computation: no users with recent feedback")
            return

        logger.info("Computing patterns for %d users with recent feedback", len(user_ids))

        computed = 0
        for user_id in user_ids:
            try:
                service = PatternLearningService(user_id)
                patterns = await service.compute_patterns()
                computed += 1
                logger.debug(
                    "Computed patterns for user %d: %d responsive hours, %d item types",
                    user_id,
                    len(patterns.get('responsive_hours', [])),
                    len(patterns.get('item_type_preferences', {})),
                )
            except Exception as e:
                logger.error("Error computing patterns for user %d: %r", user_id, e)

        logger.info("Pattern computation complete: %d/%d users processed", computed, len(user_ids))

    except Exception as e:
        logger.error("Pattern computation job error: %r", e)


async def run_embed_new_items():
    from web.services.embedding_service import get_embedding_service
    try:
        svc = get_embedding_service()
        result = await svc.embed_new_items()
        total = result.get("total", 0)
        if total > 0:
            logger.info(f"embed_new_items job: {total} new items embedded")
    except Exception as e:
        logger.error(f"embed_new_items job failed: {repr(e)}")


async def send_pending_action_notifications():
    """
    Runs every 5 minutes. For each user with unnotified pending actions,
    sends a single batched message on their configured channel (Telegram or Slack).

    Batching: all unnotified actions for a user in the current run are collapsed
    into one message — avoids spam when Claude creates multiple actions in one session.

    Skips silently (with warning log) if configured channel has no linked account.
    """
    from web.core.database import (
        get_users_with_unnotified_pending_actions,
        get_unnotified_pending_actions,
        mark_pending_action_notified,
        get_nudge_preferences,
        get_telegram_bot_user_links_for_user,
        list_slack_bot_conversations,
    )

    app_url = os.environ.get('APP_URL', 'http://localhost:8000').rstrip('/')
    actions_link = f"{app_url}/actions"

    try:
        user_ids = get_users_with_unnotified_pending_actions()
    except Exception as e:
        logger.error("send_pending_action_notifications: failed to fetch users: %s", repr(e))
        return

    for user_id in user_ids:
        try:
            prefs = get_nudge_preferences(user_id)
            channel = prefs.get('pending_action_notification_channel', 'none')

            if channel == 'none':
                # User opted out — mark actions as notified so they don't pile up
                actions = get_unnotified_pending_actions(user_id)
                for action in actions:
                    mark_pending_action_notified(action['id'], 'none')
                continue

            actions = get_unnotified_pending_actions(user_id)
            if not actions:
                continue

            # Build batched message
            count = len(actions)
            if count == 1:
                title = actions[0].get('title') or actions[0].get('action_type', 'action')
                body = f"\U0001f4cb New pending action: \"{title}\"\n\nReview and approve: {actions_link}"
            else:
                body = f"\U0001f4cb {count} new pending actions ready for your review:\n\n"
                for a in actions[:5]:  # cap preview at 5
                    title = a.get('title') or a.get('action_type', 'action')
                    body += f"\u2022 {title}\n"
                if count > 5:
                    body += f"\u2022 ...and {count - 5} more\n"
                body += f"\nReview and approve: {actions_link}"

            sent = False

            if channel == 'telegram':
                links = get_telegram_bot_user_links_for_user(user_id)
                if not links:
                    logger.warning(
                        "send_pending_action_notifications: user %d has channel='telegram' "
                        "but no linked Telegram chat — skipping", user_id
                    )
                else:
                    chat_id = links[0]['telegram_chat_id']
                    from web.services.telegram_bot_service import TelegramBotService
                    bot = TelegramBotService()
                    result = await bot.send_message(chat_id, body)
                    sent = bool(result)

            elif channel == 'slack':
                convos = list_slack_bot_conversations(user_id)
                if not convos:
                    logger.warning(
                        "send_pending_action_notifications: user %d has channel='slack' "
                        "but no linked Slack DM — skipping", user_id
                    )
                else:
                    slack_channel_id = convos[0]['slack_channel_id']
                    from web.services.slack_bot_service import SlackBotService
                    bot = SlackBotService(user_id)
                    result = await bot.send_message(slack_channel_id, body)
                    sent = bool(result)

            if sent:
                for action in actions:
                    mark_pending_action_notified(action['id'], channel)
                logger.info(
                    "send_pending_action_notifications: sent %d action(s) via %s for user %d",
                    count, channel, user_id
                )

        except Exception as e:
            logger.error(
                "send_pending_action_notifications: error for user %d: %s", user_id, repr(e)
            )

    from web.core.database import update_heartbeat as _update_heartbeat
    _update_heartbeat("pending-action-notifications")


async def _run_nightly_research():
    """4am nightly audit — measures feedback absorption fidelity for all users."""
    try:
        from web.services.nightly_research_service import NightlyResearchService
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users")
            user_ids = [row['id'] for row in cursor.fetchall()]
        for uid in user_ids:
            await NightlyResearchService(uid).run_audit()
        from web.core.database import update_heartbeat as _update_heartbeat
        _update_heartbeat("nightly-research")
    except Exception as e:
        logger.error("[nightly_research] scheduler job failed: %s", repr(e))


async def check_system_health_and_alert():
    """
    Hourly job: checks all monitored subsystems for staleness.
    Sends one Telegram + Slack DM listing red subsystems if any.

    Grace period: skips for 1 hour after app startup to avoid false alarms post-deploy.
    Dedup: stores last_alerted_at per subsystem in system_heartbeats; skips if ALL
    currently-red subsystems were alerted within the last 4 hours.

    Phase 70 — Health Monitor & Dashboard.
    """
    global _STARTUP_TIME
    try:
        from datetime import datetime as _datetime, timedelta as _timedelta
        from web.core.database import get_db as _get_db, get_system_health as _get_system_health
        from web.services.telegram_bot_service import TelegramBotService as _TelegramBotService
        from web.services.slack_bot_service import SlackBotService as _SlackBotService
        from web.core.database import get_telegram_bot_user_links_for_user as _get_tg_links

        # Startup grace: skip for first 60 minutes after deploy
        if _STARTUP_TIME is not None:
            uptime_minutes = (_datetime.utcnow() - _STARTUP_TIME).total_seconds() / 60
            if uptime_minutes < 60:
                logger.debug("[health_alert] Skipping — only %.0f min since startup", uptime_minutes)
                return

        THRESHOLDS = {
            "drip-nudges": 30,
            "urgent-nudges": 30,
            "daily-digest": 1440,
            "inbound-classification": 60,
            "pending-action-notifications": 20,
            "people-auto-tracker": 45,
            "scanner-gmail": 20,
            "scanner-telegram": 20,
            "nightly-research": 1440,
            "email-draft-scanner": 480,
        }

        now = _datetime.utcnow()
        rows = _get_system_health()
        seen = {r["subsystem"]: r for r in rows}

        red_subsystems = []
        for name, threshold in THRESHOLDS.items():
            row = seen.get(name)
            if row is None:
                red_subsystems.append(name)
                continue
            last_run = row.get("last_run_at")
            if last_run is None:
                red_subsystems.append(name)
                continue
            if isinstance(last_run, str):
                last_run_dt = _datetime.fromisoformat(last_run.replace("Z", "+00:00")).replace(tzinfo=None)
            else:
                last_run_dt = last_run
            minutes_ago = (now - last_run_dt).total_seconds() / 60
            if minutes_ago > threshold * 1.5:
                red_subsystems.append(name)

        if not red_subsystems:
            logger.debug("[health_alert] All subsystems healthy")
            return

        # Dedup: skip if all red subsystems were alerted within 4 hours
        dedup_threshold = now - _timedelta(hours=4)
        all_recently_alerted = True
        for name in red_subsystems:
            row = seen.get(name)
            if row is None:
                all_recently_alerted = False
                break
            last_alerted = row.get("last_alerted_at")
            if last_alerted is None:
                all_recently_alerted = False
                break
            if isinstance(last_alerted, str):
                last_alerted_dt = _datetime.fromisoformat(last_alerted.replace("Z", "+00:00")).replace(tzinfo=None)
            else:
                last_alerted_dt = last_alerted
            if last_alerted_dt < dedup_threshold:
                all_recently_alerted = False
                break

        if all_recently_alerted:
            logger.debug("[health_alert] All red subsystems already alerted within 4h — skipping")
            return

        # Build alert message
        lines = ["\u26a0\ufe0f *Seny health alert*\n\nThese background jobs have gone silent:"]
        for name in sorted(red_subsystems):
            row = seen.get(name)
            if row and row.get("last_run_at"):
                last_run = row["last_run_at"]
                if isinstance(last_run, str):
                    last_run_dt = _datetime.fromisoformat(last_run.replace("Z", "+00:00")).replace(tzinfo=None)
                else:
                    last_run_dt = last_run
                mins = int((now - last_run_dt).total_seconds() / 60)
                lines.append(f"\u2022 {name} \u2014 last ran {mins}m ago")
            else:
                lines.append(f"\u2022 {name} \u2014 never ran")
        lines.append("\nCheck Railway logs for errors.")
        msg = "\n".join(lines)

        # Update last_alerted_at for all red subsystems
        try:
            with _get_db() as conn:
                cursor = conn.cursor()
                for name in red_subsystems:
                    cursor.execute("""
                        INSERT INTO system_heartbeats (subsystem, last_run_at, last_alerted_at, updated_at)
                        VALUES (%s, NULL, NOW(), NOW())
                        ON CONFLICT (subsystem) DO UPDATE
                        SET last_alerted_at = NOW(), updated_at = NOW()
                    """, (name,))
        except Exception as e:
            logger.error("[health_alert] Failed to update last_alerted_at: %r", e)

        # Get all users and send alerts
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users")
            user_ids = [row['id'] for row in cursor.fetchall()]

        for uid in user_ids:
            # Telegram
            try:
                tg_links = _get_tg_links(uid)
                for link in tg_links:
                    try:
                        await _TelegramBotService().send_message(link['telegram_chat_id'], msg)
                    except Exception as e:
                        logger.warning("[health_alert] Telegram send failed: %r", e)
            except Exception as e:
                logger.warning("[health_alert] Telegram lookup failed for user %d: %r", uid, e)

            # Slack
            try:
                with _get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT authed_user_id, bot_token FROM slack_tokens WHERE user_id = %s AND bot_token IS NOT NULL LIMIT 1",
                        (uid,)
                    )
                    slack_row = cursor.fetchone()
                if slack_row and slack_row['bot_token'] and slack_row['bot_token'].startswith('xoxb-'):
                    slack_bot = _SlackBotService(uid)
                    channel_id = await slack_bot.get_bot_dm_channel(slack_row['authed_user_id'])
                    if channel_id:
                        await slack_bot.send_message(channel_id, msg)
            except Exception as e:
                logger.warning("[health_alert] Slack send failed for user %d: %r", uid, e)

        logger.info("[health_alert] Alerted for %d red subsystems: %s", len(red_subsystems), red_subsystems)

    except Exception as e:
        logger.error("[health_alert] Job failed: %r", e)


async def _send_research_notification():
    """
    Hourly job: sends Telegram + Slack DM at 2pm local time when research_proposals are pending.

    Uses IntervalTrigger(minutes=60) + per-user timezone check — NOT CronTrigger(hour=14).
    All imports are local to avoid circular import issues.
    """
    try:
        from datetime import datetime as _datetime
        from zoneinfo import ZoneInfo as _ZoneInfo
        from web.core.database import get_db as _get_db, get_last_audit_run as _get_last_audit_run
        from web.core.database import get_telegram_bot_user_links_for_user as _get_telegram_links
        from web.services.telegram_bot_service import TelegramBotService as _TelegramBotService
        from web.services.slack_bot_service import SlackBotService as _SlackBotService

        # Get all user IDs
        with _get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users")
            user_ids = [row['id'] for row in cursor.fetchall()]

        now_utc = _datetime.utcnow()

        for uid in user_ids:
            try:
                # Get user's digest timezone preference
                with _get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT digest_timezone FROM user_settings WHERE user_id = %s",
                        (uid,)
                    )
                    row = cursor.fetchone()
                    tz_name = (row['digest_timezone'] if row and row['digest_timezone'] else 'America/Chicago')

                # Convert UTC to user local time
                user_tz = _ZoneInfo(tz_name)
                user_local = now_utc.replace(tzinfo=_ZoneInfo('UTC')).astimezone(user_tz)
                user_local_hour = user_local.hour

                # Only proceed at 2pm local
                if user_local_hour != 14:
                    continue

                # Count pending research_proposals
                with _get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT COUNT(*) FROM pending_actions WHERE user_id = %s AND status = 'pending' AND action_type = 'research_proposal'",
                        (uid,)
                    )
                    count_row = cursor.fetchone()
                    count = count_row[0] if count_row else 0

                if count == 0:
                    continue

                # Get latest audit for fidelity score
                audit = _get_last_audit_run(uid)
                if audit:
                    fidelity_pct = int(audit['fidelity_score'] * 100)
                else:
                    fidelity_pct = "N/A"

                msg = (
                    f"🧪 *{count} memory proposal{'s' if count != 1 else ''} ready*\n"
                    f"Fidelity score: {fidelity_pct}%\n"
                    f"Open the Actions tab to review."
                )

                # Send Telegram notifications
                try:
                    tg_links = _get_telegram_links(uid)
                    for link in tg_links:
                        try:
                            await _TelegramBotService().send_message(link['telegram_chat_id'], msg)
                        except Exception as e:
                            logger.warning("[research_notification] Telegram send failed for user %d chat %s: %s", uid, link['telegram_chat_id'], repr(e))
                except Exception as e:
                    logger.warning("[research_notification] Telegram setup failed for user %d: %s", uid, repr(e))

                # Send Slack DM notification
                try:
                    with _get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT authed_user_id, bot_token FROM slack_tokens WHERE user_id = %s AND bot_token IS NOT NULL LIMIT 1",
                            (uid,)
                        )
                        slack_row = cursor.fetchone()

                    if slack_row and slack_row['bot_token'] and slack_row['bot_token'].startswith('xoxb-'):
                        slack_bot = _SlackBotService(uid)
                        channel_id = await slack_bot.get_bot_dm_channel(slack_row['authed_user_id'])
                        if channel_id:
                            await slack_bot.send_message(channel_id, msg)
                    elif slack_row and slack_row['bot_token'] and not slack_row['bot_token'].startswith('xoxb-'):
                        logger.warning("[research_notification] Skipping xoxp- token for user %d (only xoxb- valid)", uid)
                except Exception as e:
                    logger.warning("[research_notification] Slack DM failed for user %d: %s", uid, repr(e))

            except Exception as e:
                logger.warning("[research_notification] Failed for user %d: %s", uid, repr(e))

    except Exception as e:
        logger.error("[research_notification] scheduler job failed: %s", repr(e))


def start_scheduler():
    """
    Start the background notification scheduler.

    Call this from the FastAPI startup event.
    """
    global scheduler, _STARTUP_TIME
    _STARTUP_TIME = datetime.utcnow()

    if scheduler is not None:
        logger.warning("Scheduler already running")
        return

    scheduler = AsyncIOScheduler()

    # Add notification processing job - runs every 90 seconds
    scheduler.add_job(
        notification_job,
        IntervalTrigger(seconds=90),
        id='notification_processor',
        replace_existing=True,
        max_instances=1,  # Only one instance at a time
        coalesce=True  # Combine missed runs
    )

    # Add daily digest processing job - runs hourly at minute 0
    scheduler.add_job(
        process_daily_digests,
        CronTrigger(minute=0),  # Every hour at :00
        id='digest_processor',
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )

    # Add weekly review processing job - runs hourly at minute 5
    # Offset by 5 minutes from digest to avoid concurrent processing
    scheduler.add_job(
        process_weekly_reviews,
        CronTrigger(minute=5),  # Every hour at :05
        id='weekly_review_processor',
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )

    # Add Drive auto-sync job - runs every 4 hours at :30
    scheduler.add_job(
        process_drive_sync,
        CronTrigger(hour='*/4', minute=30),
        id='drive_sync_processor',
        name='Drive auto-sync (every 4 hours)',
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Nightly research audit - 4am daily
    scheduler.add_job(
        _run_nightly_research,
        CronTrigger(hour=4, minute=0),
        id='nightly_research',
        name='Nightly Research Audit (4am)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Research notification - hourly check, fires at 2pm per user local time
    scheduler.add_job(
        _send_research_notification,
        IntervalTrigger(minutes=60),
        id='research_notification',
        name='Research Notification (2pm local time check)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # System health heartbeat — hourly job; alerts via Telegram+Slack if any subsystem goes silent
    # Health Monitor & Dashboard
    scheduler.add_job(
        check_system_health_and_alert,
        IntervalTrigger(minutes=60),
        id='system_health_heartbeat',
        name='System Health Heartbeat (hourly)',
        replace_existing=True,
        misfire_grace_time=300,
    )

    # ========================================================================
    # Scanner Jobs - (Scanner Engine)
    # User-configurable sources poll every 5 min; actual scan frequency is
    # controlled by user preferences via get_scanner_interval_for_source()
    # ========================================================================

    # Gmail: polls every 5 min, user controls actual interval (default 15 min)
    scheduler.add_job(
        _scan_gmail,
        IntervalTrigger(minutes=5, jitter=30),
        id='scanner_gmail',
        name='Scanner: Gmail (checks every 5 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Note: Slack scanner job removed in HF-08 — replaced by continuous drip loop
    # (started in web/main.py via slack_drip_service.start_drip_loop())

    # Telegram: polls every 5 min, user controls actual interval (default 5 min)
    scheduler.add_job(
        _scan_telegram,
        IntervalTrigger(minutes=5, jitter=30),
        id='scanner_telegram',
        name='Scanner: Telegram (checks every 5 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Calendar: polls every 5 min, user controls actual interval (default 60 min)
    scheduler.add_job(
        _scan_calendar,
        IntervalTrigger(minutes=5, jitter=30),
        id='scanner_calendar',
        name='Scanner: Calendar (checks every 5 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Drive scanner: same interval as drive_sync (every 4 hours)
    scheduler.add_job(
        _scan_drive_scanner,
        CronTrigger(hour='*/4', minute=35),
        id='scanner_drive',
        name='Scanner: Drive (every 4 hours)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Contacts: every 60 minutes
    scheduler.add_job(
        _scan_contacts,
        IntervalTrigger(minutes=60, jitter=60),
        id='scanner_contacts',
        name='Scanner: Contacts (every 60 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Notes: every 6 hours
    scheduler.add_job(
        _scan_notes,
        CronTrigger(hour='*/6', minute=10),
        id='scanner_notes',
        name='Scanner: Notes (every 6 hours)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Tasks: every 6 hours
    scheduler.add_job(
        _scan_tasks,
        CronTrigger(hour='*/6', minute=15),
        id='scanner_tasks',
        name='Scanner: Tasks (every 6 hours)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Location: daily at 2am
    scheduler.add_job(
        _scan_location,
        CronTrigger(hour=2, minute=0),
        id='scanner_location',
        name='Scanner: Location (daily at 2am)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Conversations: every 60 minutes
    scheduler.add_job(
        _scan_conversations,
        IntervalTrigger(minutes=60, jitter=60),
        id='scanner_conversations',
        name='Scanner: Conversations (every 60 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Entity resolution: every 30 minutes
    scheduler.add_job(
        process_entity_resolution,
        CronTrigger(minute='*/30'),
        id='entity_resolution',
        name='Entity Resolution (every 30 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Inbound classification processing: every 20 minutes
    # Runs after scanner sources typically complete, classifies new items
    scheduler.add_job(
        process_inbound_classification,
        IntervalTrigger(minutes=20, jitter=60),
        id='inbound_classification',
        name='Inbound Classification (every 20 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ========================================================================
    # Nudge Jobs - (Autonomous Nudges)
    # ========================================================================

    # Urgent nudge processing: every 10 minutes
    # Processes pending detected_actions, urgent classifications, overdue tasks
    scheduler.add_job(
        process_urgent_nudges,
        IntervalTrigger(minutes=10, jitter=30),
        id='urgent_nudge_processor',
        name='Urgent Nudges (every 10 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Batch nudge processing: hourly at :30
    # Sends batched normal-priority nudges as mini-digests
    scheduler.add_job(
        process_batch_nudges,
        CronTrigger(minute=30),
        id='batch_nudge_processor',
        name='Batch Nudges (hourly at :30)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Drip nudge processing: every 15 minutes
    # Sends one conversational check-in at a time per user
    scheduler.add_job(
        process_drip_nudges,
        IntervalTrigger(minutes=15, jitter=60),
        id='drip_nudge_processor',
        name='Drip Nudges (every 15 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduler: Drip nudge job registered (every 15 min)")

    # Full scan sweep: every 4 hours
    scheduler.add_job(
        process_full_scan,
        CronTrigger(hour='*/4', minute=45),
        id='full_scan',
        name='Full Scan Sweep (every 4 hours)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ========================================================================
    # Pattern Learning Jobs - (User Pattern Learning)
    # ========================================================================

    # Pattern computation: daily at 3:00 AM
    # Recomputes user patterns from recent feedback for personalized nudges
    scheduler.add_job(
        compute_user_patterns,
        CronTrigger(hour=3, minute=0),
        id='compute_user_patterns',
        name='Pattern Computation (daily at 3am)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ========================================================================
    # People Auto-Tracker Jobs - (Automatic People Tracker)
    # ========================================================================

    # People auto-tracker: every 15 minutes
    # Updates last_contact_date and extracts context from communications
    scheduler.add_job(
        process_people_auto_tracker,
        IntervalTrigger(minutes=15, jitter=60),
        id='people_auto_tracker',
        name='People Auto-Tracker (every 15 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ========================================================================
    # Predictive Intelligence Jobs - (Meeting Prep)
    # ========================================================================

    # Meeting prep briefings: every 15 minutes
    # Sends context briefs 30-90 min before meetings with attendees
    scheduler.add_job(
        process_meeting_prep,
        IntervalTrigger(minutes=15, jitter=60),
        id='meeting_prep',
        name='Meeting Prep Briefings (every 15 min)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
        coalesce=True,
    )

    # Relationship & follow-up predictions: daily at 9:00 AM
    # Reminds user of stale contacts and open follow-up items
    scheduler.add_job(
        process_relationship_predictions,
        'cron',
        hour=9,
        minute=0,
        id='relationship_predictions',
        name='Relationship & Follow-up Predictions (daily 9am)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Nudge follow-up loop: every 4 hours
    # Sends gentle follow-ups for overdue_task nudges with no response within 4–24h
    scheduler.add_job(
        process_nudge_followups,
        IntervalTrigger(hours=4, jitter=300),
        id='nudge_followups',
        name='Nudge Follow-up Loop (every 4 hours)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ========================================================================
    # Smart Forward-Looking Nudges
    # ========================================================================

    # Upcoming task nudges: every 30 minutes
    # Nudges about tasks due within the priority-based lead window (4–48h out)
    scheduler.add_job(
        process_upcoming_task_nudges,
        IntervalTrigger(minutes=30, jitter=120),
        id='upcoming_task_nudges',
        name='Upcoming Task Nudges (every 30 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # AI coach nudges: every 2 hours
    # Uses Claude Haiku to decide what the user should focus on right now
    scheduler.add_job(
        process_ai_coach_nudges,
        IntervalTrigger(hours=2, jitter=300),
        id='ai_coach_nudges',
        name='AI Coach Nudges (every 2 hours)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
        coalesce=True,
    )

    # ========================================================================
    # Embedding Jobs - (Vector Embeddings)
    # ========================================================================

    # Embed new items: every 30 minutes (only when Voyage API key is configured)
    if os.getenv("VOYAGE_API_KEY"):
        scheduler.add_job(
            run_embed_new_items,
            IntervalTrigger(minutes=30, jitter=60),
            id='embed_new_items',
            name='Embed New Items (every 30 min)',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduler: Embedding job registered (every 30 min)")
        print("✓ Embedding job registered (every 30 min)")
    else:
        logger.info("Scheduler: Embedding job skipped (VOYAGE_API_KEY not set)")
        print("✓ Embedding job skipped (VOYAGE_API_KEY not set)")

    # ========================================================================
    # Multi-Channel Chat Jobs - (Telegram Bot, Slack Bot)
    # ========================================================================

    # Telegram bot polling: only register if webhook mode is NOT configured
    # When webhook is configured, Telegram sends updates directly to /api/webhooks/telegram
    telegram_webhook_configured = (
        bool(os.getenv("TELEGRAM_WEBHOOK_SECRET")) and
        bool(os.getenv("APP_URL"))
    )

    if not telegram_webhook_configured:
        # Polling mode: register job to poll every 5 seconds
        scheduler.add_job(
            _process_telegram_bot,
            IntervalTrigger(seconds=5),
            id='telegram_bot_poll',
            name='Telegram Bot Poll (every 5 sec)',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduler: Telegram bot polling job registered (every 5 sec)")
        print("✓ Telegram bot polling job registered (every 5 sec)")
    else:
        # Webhook mode: skip polling job
        logger.info("Scheduler: Telegram bot using webhook mode, polling job skipped")
        print("✓ Telegram bot using webhook mode (polling disabled)")

    # Slack bot polling: only register if Events API is NOT configured
    # When SLACK_SIGNING_SECRET is set, Slack sends DMs via Events API webhook
    # When not set, fall back to polling every 10 seconds
    slack_events_configured = bool(os.getenv("SLACK_SIGNING_SECRET"))

    if not slack_events_configured:
        scheduler.add_job(
            _process_slack_bot,
            IntervalTrigger(seconds=10),
            id='slack_bot_poll',
            name='Slack Bot Poll (every 10 sec)',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("Scheduler: Slack bot polling job registered (every 10 sec)")
        print("✓ Slack bot polling job registered (every 10 sec)")
    else:
        logger.info("Scheduler: Slack using Events API (no polling)")
        print("✓ Slack bot using Events API mode (polling disabled)")

    # ========================================================================
    # Calendar → Nudge Bridge
    # ========================================================================

    # Sync upcoming calendar events and schedule nudge sequences: daily at 14:00 UTC
    # (runs before the user's ~3pm day start; idempotent — safe to run daily)
    scheduler.add_job(
        sync_upcoming_calendar_nudges,
        CronTrigger(hour=14, minute=0),
        id='calendar_nudge_sync',
        name='Calendar Nudge Sync (daily 14:00 UTC)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("Scheduler: Calendar nudge sync job registered (daily 14:00 UTC)")

    # Fire due calendar event nudges: every 10 minutes
    scheduler.add_job(
        process_calendar_event_nudges,
        IntervalTrigger(minutes=10, jitter=30),
        id='calendar_nudge_processor',
        name='Calendar Nudge Processor (every 10 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduler: Calendar nudge processor registered (every 10 min)")

    # ========================================================================
    # Email Draft Scanner — Email Drafting
    # ========================================================================

    # Email draft proposals: every 6 hours
    # Finds Gmail emails needing reply (last 7 days), creates pending_action drafts
    from web.services.email_draft_scanner import process_email_draft_proposals
    scheduler.add_job(
        process_email_draft_proposals,
        CronTrigger(hour='*/6', minute=20),
        id='email_draft_scanner',
        name='Email Draft Scanner (every 6 hours)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduler: Email draft scanner job registered (every 6 hours)")

    # Pending action notifications: every 5 minutes
    # Batches unnotified pending actions and delivers to configured channel
    scheduler.add_job(
        send_pending_action_notifications,
        IntervalTrigger(minutes=5, jitter=30),
        id='pending_action_notifications',
        name='Pending Action Notifications (every 5 min)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduler: Pending action notification job registered (every 5 min)")

    scheduler.start()
    logger.info("Notification scheduler started (90-second interval)")
    logger.info("Daily digest scheduler started (hourly at :00)")
    logger.info("Weekly review scheduler started (hourly at :05)")
    logger.info("Scheduler: Drive auto-sync job registered (every 4 hours at :30)")
    logger.info("Scheduler: Scanner jobs registered (9 sources via APScheduler + Slack via drip loop)")
    logger.info("Scheduler: Inbound classification job registered (every 20 min)")
    logger.info("Scheduler: Nudge jobs registered (urgent every 10 min, batch hourly at :30, upcoming every 30 min, AI coach every 2h)")
    logger.info("Scheduler: Pattern computation job registered (daily at 3am)")
    logger.info("Scheduler: People auto-tracker job registered (every 15 min)")
    logger.info("Scheduler: Meeting prep briefing job registered (every 15 min)")
    logger.info("Scheduler: Email draft scanner job registered (every 6 hours at :20)")
    # Note: Telegram and Slack bot modes (polling vs webhook) logged above where decided
    print("✓ Notification scheduler started (90-second interval)")
    print("✓ Daily digest scheduler started (hourly at :00)")
    print("✓ Weekly review scheduler started (hourly at :05)")
    print("✓ Drive auto-sync scheduler started (every 4 hours at :30)")
    print("✓ Scanner jobs registered (9 sources via APScheduler + Slack via drip loop)")
    print("✓ Inbound classification job registered (every 20 min)")
    print("✓ Nudge jobs registered (urgent every 10 min, batch hourly at :30, upcoming every 30 min, AI coach every 2h)")
    print("✓ Pattern computation job registered (daily at 3am)")
    print("✓ People auto-tracker job registered (every 15 min)")
    print("✓ Meeting prep briefing job registered (every 15 min)")
    print("✓ Phase 39: Calendar nudge sync (daily) + processor (every 10 min) registered")
    print("✓ Phase 45: Email draft scanner job registered (every 6 hours at :20)")
    print("✓ Pending action notification job registered (every 5 min)")
    # Note: Telegram and Slack bot modes (polling vs webhook) printed above where decided


def stop_scheduler():
    """
    Stop the background notification scheduler.

    Call this from the FastAPI shutdown event.
    """
    global scheduler

    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("Notification scheduler stopped")
        print("✓ Notification scheduler stopped")
