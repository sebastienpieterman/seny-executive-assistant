"""
QA Trigger API — Phase 63

POST /api/qa/triggers/{job_name}   — run any background job on demand
POST /api/qa/inject/scanned-item   — inject a raw scanned item
POST /api/qa/inject/scheduling-email — inject a pre-built scheduling email
DELETE /api/qa/inject/cleanup      — remove all qa_inject test data
GET  /api/qa/baseline              — raw counts + pipeline health metrics

All endpoints require authentication.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import create_pending_action, get_db, insert_scanned_item, list_google_tokens, record_feedback
from web.core.scheduler import (
    compute_user_patterns,
    process_batch_nudges,
    process_daily_digests,
    process_drip_nudges,
    process_meeting_prep,
    process_nudge_followups,
    process_people_auto_tracker,
    process_relationship_predictions,
    process_urgent_nudges,
    process_weekly_reviews,
    send_pending_action_notifications,
    sync_calendar_reminders,
    fire_calendar_reminders,
)
from web.services.email_draft_scanner import process_email_draft_proposals
from web.services.nightly_research_service import NightlyResearchService

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Per-job asyncio locks — prevent concurrent executions of the same job
# ---------------------------------------------------------------------------

_job_locks: dict[str, asyncio.Lock] = {}


def _get_lock(job: str) -> asyncio.Lock:
    if job not in _job_locks:
        _job_locks[job] = asyncio.Lock()
    return _job_locks[job]


# ---------------------------------------------------------------------------
# Standard jobs (no extra arguments required)
# ---------------------------------------------------------------------------

JOBS: dict[str, object] = {
    "drip-nudges": process_drip_nudges,
    "urgent-nudges": process_urgent_nudges,
    "batch-nudges": process_batch_nudges,
    "calendar-reminder-sync": sync_calendar_reminders,
    "calendar-reminder-fire": fire_calendar_reminders,
    "email-draft-scanner": process_email_draft_proposals,
    "pending-action-notifications": send_pending_action_notifications,
    "people-auto-tracker": process_people_auto_tracker,
    "meeting-prep": process_meeting_prep,
    "pattern-computation": compute_user_patterns,
    "relationship-predictions": process_relationship_predictions,
    "nudge-followups": process_nudge_followups,
    "daily-digest": process_daily_digests,
    "weekly-review": process_weekly_reviews,
}

# nightly-research requires user_id so it is handled as a special case below
_ALL_JOB_SLUGS = sorted(list(JOBS.keys()) + ["nightly-research"])


# ---------------------------------------------------------------------------
# Trigger endpoint
# ---------------------------------------------------------------------------

@router.post("/triggers/{job_name}")
async def trigger_job(job_name: str, user_id: int = Depends(require_auth)):
    """
    Run a background job synchronously and return a result summary.

    Returns:
        {"job": str, "status": "completed"|"error"|"already_running",
         "duration_seconds": float, "error": str|null}
    """
    if job_name not in JOBS and job_name != "nightly-research":
        raise HTTPException(
            status_code=404,
            detail=f"Unknown job: {job_name}. Valid jobs: {_ALL_JOB_SLUGS}",
        )

    lock = _get_lock(job_name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        return {
            "job": job_name,
            "status": "already_running",
            "duration_seconds": 0,
            "error": None,
        }

    start = time.monotonic()
    try:
        if job_name == "nightly-research":
            await NightlyResearchService(user_id).run_audit()
        else:
            await JOBS[job_name]()

        return {
            "job": job_name,
            "status": "completed",
            "duration_seconds": round(time.monotonic() - start, 2),
            "error": None,
        }
    except Exception as exc:
        logger.exception("QA trigger %s failed", job_name)
        return {
            "job": job_name,
            "status": "error",
            "duration_seconds": round(time.monotonic() - start, 2),
            "error": repr(exc),
        }
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Data injection endpoints
# ---------------------------------------------------------------------------

class InjectScannedItemRequest(BaseModel):
    content: str
    sender: str = "qa-test@example.com"
    item_type: str = "email"
    direction: str = "inbound"


def _create_qa_scanner_run(user_id: int) -> int:
    """Insert a scanner_run record with source='qa_inject' and return its id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO scanner_runs (user_id, source, started_at, completed_at, status, items_found, items_new)
            VALUES (%s, 'qa_inject', NOW(), NOW(), 'completed', 1, 1)
            RETURNING id
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        return row["id"]


@router.post("/inject/scanned-item")
async def inject_scanned_item(
    body: InjectScannedItemRequest,
    user_id: int = Depends(require_auth),
):
    """
    Inject a raw scanned item tagged source='qa_inject'.
    Run POST /api/inbound/process-now to classify it.
    """
    source_id = f"qa_{uuid4().hex[:12]}"
    source_metadata = json.dumps({
        "content": body.content,
        "sender": body.sender,
        "qa_inject": True,
    })

    run_id = _create_qa_scanner_run(user_id)
    item_id = insert_scanned_item(
        user_id=user_id,
        scanner_run_id=run_id,
        source="qa_inject",
        source_id=source_id,
        source_metadata=source_metadata,
        item_type=body.item_type,
        direction=body.direction,
    )

    if item_id is None:
        raise HTTPException(status_code=409, detail="Duplicate item — source_id already exists.")

    return {
        "item_id": item_id,
        "source": "qa_inject",
        "source_id": source_id,
        "message": "Injected. Run POST /api/inbound/process-now to classify it.",
    }


@router.post("/inject/scheduling-email")
async def inject_scheduling_email(user_id: int = Depends(require_auth)):
    """
    Inject a pre-built scheduling email for testing SchedulingExtractor.
    Run POST /api/inbound/process-now then POST /api/qa/triggers/email-draft-scanner.
    """
    content = (
        "Subject: Quick sync this week?\n\n"
        "Hi, are you free to meet Thursday at 2pm to go over the project status?\n"
        "Let me know if that works or suggest another time."
    )
    source_id = f"qa_scheduling_{uuid4().hex[:8]}"
    thread_id = f"qa_thread_{uuid4().hex[:8]}"
    source_metadata = json.dumps({
        "snippet": content,
        "from": "test-contact@example.com",
        "subject": "Quick sync this week?",
        "thread_id": thread_id,
        "qa_inject": True,
    })

    run_id = _create_qa_scanner_run(user_id)
    item_id = insert_scanned_item(
        user_id=user_id,
        scanner_run_id=run_id,
        source="qa_inject",
        source_id=source_id,
        source_metadata=source_metadata,
        item_type="email",
        direction="inbound",
    )

    if item_id is None:
        raise HTTPException(status_code=409, detail="Duplicate item — try again.")

    return {
        "item_id": item_id,
        "source_id": source_id,
        "message": (
            "Scheduling email injected. "
            "Run POST /api/inbound/process-now to classify it, "
            "then check GET /api/inbound/actions for items with type='calendar_proposal'."
        ),
    }


@router.post("/inject/email-draft")
async def inject_email_draft(user_id: int = Depends(require_auth)):
    """
    Inject a safe email_draft pending action addressed to the user's own email.
    Tests the approval pipeline without emailing real contacts.
    Cleanup removes all rows with source='qa_inject'.
    """
    tokens = list_google_tokens(user_id)
    if not tokens:
        raise HTTPException(
            status_code=400,
            detail="No Gmail account linked — inject/email-draft requires a connected Google account",
        )
    user_email = tokens[0]["email"]
    content = {
        "to": user_email,
        "cc": None,
        "subject": "QA Test: Pending Actions Verification",
        "body": "This is an automated QA test email verifying the pending actions approval pipeline. Safe to delete.",
        "thread_id": None,
        "gmail_account": user_email,
    }
    action_id = create_pending_action(
        user_id,
        "email_draft",
        "QA Test: email draft approval",
        json.dumps(content),
        source="qa_inject",
        source_ref="qa_email_draft_test",
    )
    if action_id is None:
        raise HTTPException(status_code=500, detail="Failed to create pending action")
    return {"action_id": action_id, "to": user_email}


class InjectNudgeRequest(BaseModel):
    title: str = "QA Test: follow up on project sync"
    nudge_type: str = "qa_inject"
    age_hours: float = 0  # If > 0, backdates created_at by this many hours


@router.post("/inject/nudge")
async def inject_nudge(req: InjectNudgeRequest, user_id: int = Depends(require_auth)):
    """
    Insert a pending nudge row for drip testing.

    nudge_type defaults to 'qa_inject' (safe, cleanup removes it).
    Set nudge_type='meeting_prep' and age_hours=48 to test stale-calendar filtering.
    Cleanup removes all rows with nudge_type='qa_inject'.
    """
    from datetime import timezone as _tz_inject, timedelta as _td_inject
    created_at = datetime.now(_tz_inject.utc)
    if req.age_hours > 0:
        created_at = created_at - _td_inject(hours=req.age_hours)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO nudges (user_id, nudge_type, channel, title, urgency, status, created_at)
            VALUES (%s, %s, 'telegram', %s, 'urgent', 'pending', %s)
            RETURNING id
            """,
            (user_id, req.nudge_type, req.title, created_at),
        )
        row = cursor.fetchone()
        nudge_id = row['id'] if row else None
        conn.commit()
    return {
        "nudge_id": nudge_id,
        "nudge_type": req.nudge_type,
        "status": "pending",
        "created_at": created_at.isoformat(),
        "age_hours": req.age_hours,
    }


@router.post("/drip-nudge-now")
async def drip_nudge_now(user_id: int = Depends(require_auth)):
    """
    Force-fire one drip nudge for the authenticated user.

    Bypasses: quiet hours, drip interval, nudge_enabled, user_status, _recent_screen_nudge.
    Does NOT bypass is_nudge_stale — stale nudges are skipped and marked batched so the
    next fresh nudge in the queue can be tested.

    Returns {sent, nudge_id, channel, reason} — actual delivery result plus debug context.
    """
    from web.core.database import get_next_drip_nudge
    from web.services.nudge_service import NudgeService

    service = NudgeService(user_id)

    # Skip stale nudges (up to 5 attempts) so we reach a deliverable one
    item = None
    for _ in range(5):
        candidate = get_next_drip_nudge(user_id)
        if not candidate:
            break
        stale, stale_reason = await service.is_nudge_stale(candidate)
        if stale:
            # Mark as batched so it leaves the queue
            with get_db() as conn:
                conn.cursor().execute(
                    "UPDATE nudges SET status = 'batched' WHERE id = %s",
                    (candidate['id'],),
                )
            continue
        item = candidate
        break

    if not item:
        return {"sent": False, "nudge_id": None, "channel": None, "reason": "no_pending_nudges"}

    body = service.format_drip_message(item)
    send_result = await service.send_nudge(
        nudge_type=item.get('nudge_type', 'drip'),
        title=body,
        body=None,
        urgency='normal',
        source_type=item.get('source_type'),
        source_id=item.get('source_id'),
    )

    if send_result.get('success'):
        # Mark source nudge as batched so it doesn't re-drip
        if item.get('id'):
            with get_db() as conn:
                conn.cursor().execute(
                    "UPDATE nudges SET status = 'batched' WHERE id = %s",
                    (item['id'],),
                )
        return {
            "sent": True,
            "nudge_id": send_result.get('nudge_id'),
            "channel": send_result.get('channel'),
            "reason": "delivered",
        }

    return {
        "sent": False,
        "nudge_id": None,
        "channel": None,
        "reason": send_result.get('error', 'delivery_failed'),
    }


@router.post("/send-digest")
async def send_digest_direct(user_id: int = Depends(require_auth)):
    """Send daily digest to authenticated user immediately, bypassing hour guard."""
    from web.services.digest_service import DigestService
    start = time.monotonic()
    try:
        result = await DigestService(user_id).deliver_digest()
        return {"status": "completed", "duration_seconds": round(time.monotonic() - start, 2), **result}
    except Exception as exc:
        logger.exception("send-digest failed")
        return {"status": "error", "error": repr(exc), "duration_seconds": round(time.monotonic() - start, 2)}


@router.post("/send-weekly-review")
async def send_weekly_review_direct(user_id: int = Depends(require_auth)):
    """Send weekly review to authenticated user immediately, bypassing day/hour guard."""
    from web.services.digest_service import DigestService
    start = time.monotonic()
    try:
        result = await DigestService(user_id).deliver_weekly_review()
        return {"status": "completed", "duration_seconds": round(time.monotonic() - start, 2), **result}
    except Exception as exc:
        logger.exception("send-weekly-review failed")
        return {"status": "error", "error": repr(exc), "duration_seconds": round(time.monotonic() - start, 2)}


@router.post("/inject/calendar-event-nudge")
async def inject_calendar_event_nudge(user_id: int = Depends(require_auth)):
    """
    Insert 3 calendar_event_nudge rows (offsets -240, -60, -15 minutes) all immediately due.
    Uses event_id prefix 'qa_calendar_test_' for cleanup identification.

    Offsets mirror the real sync job (CalendarReminderService):
      -240 → "In 4 hours" nudge
      -60  → "1 hour" nudge
      -15  → "15 min" nudge
    None of these trigger the Haiku grace check (only offset==15 does).
    """
    from datetime import datetime, timedelta

    event_id = f"qa_calendar_test_{uuid4().hex[:12]}"
    event_title = "QA Test Event"
    # Event starts 5 hours from now (so -240 offset makes sense contextually)
    event_start = (datetime.utcnow() + timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:%S')
    # All rows are immediately due (2 min in the past)
    scheduled_for = (datetime.utcnow() - timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%S')

    offsets = [-240, -60, -15]
    inserted = []
    with get_db() as conn:
        cursor = conn.cursor()
        for offset in offsets:
            cursor.execute(
                """
                INSERT INTO calendar_event_nudges
                    (user_id, event_id, event_title, event_start, is_all_day,
                     offset_minutes, scheduled_for, status)
                VALUES (%s, %s, %s, %s, 0, %s, %s, 'pending')
                RETURNING id
                """,
                (user_id, event_id, event_title, event_start, offset, scheduled_for),
            )
            row = cursor.fetchone()
            if row:
                inserted.append({"id": row['id'], "offset_minutes": offset})
        conn.commit()

    return {
        "event_id": event_id,
        "rows_inserted": len(inserted),
        "rows": inserted,
        "note": "All rows scheduled_for 2 min ago — immediately due. Trigger calendar-nudge-processor to process.",
    }


@router.post("/inject/feedback")
async def inject_feedback(user_id: int = Depends(require_auth)):
    """
    Insert a test negative feedback row with a reason for nightly research testing.
    feedback_context is tagged qa_inject=True for cleanup identification.
    """
    feedback_id = record_feedback(
        user_id=user_id,
        item_type='nudge',
        item_id=None,
        feedback_type='not_helpful',
        feedback_context=json.dumps({'qa_inject': True}),
        reason='QA test: this nudge type keeps surfacing items I have already handled',
    )
    return {
        "feedback_id": feedback_id,
        "feedback_type": "not_helpful",
        "reason": "QA test reason injected",
    }


@router.delete("/inject/cleanup")
async def cleanup_injected_data(user_id: int = Depends(require_auth)):
    """
    Delete all QA-injected test data for this user in correct cascade order.
    """
    counts: dict[str, int] = {
        "pending_actions": 0,
        "item_classifications": 0,
        "detected_actions": 0,
        "scanner_runs": 0,
        "scanned_items": 0,
        "qa_nudges": 0,
        "calendar_event_nudges": 0,
        "calendar_derived_nudges": 0,
        "qa_feedback": 0,
    }

    with get_db() as conn:
        cursor = conn.cursor()

        # Collect qa_inject item IDs first
        try:
            cursor.execute(
                "SELECT id FROM scanned_items WHERE user_id = %s AND source = 'qa_inject'",
                (user_id,),
            )
            rows = cursor.fetchall()
            item_ids = [r["id"] for r in rows]
        except Exception as e:
            logger.error("QA cleanup: failed to fetch qa_inject item ids: %s", repr(e))
            item_ids = []

        # Delete qa_inject email drafts
        try:
            cursor.execute(
                "DELETE FROM pending_actions WHERE user_id = %s AND source = 'qa_inject'",
                (user_id,),
            )
            counts["pending_actions"] += cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: qa_inject pending_actions delete failed: %s", repr(e))

        # Delete pending_actions created by SchedulingExtractor for qa test threads
        # Note: %% escapes the % character in psycopg2 (% is the placeholder prefix)
        try:
            cursor.execute(
                "DELETE FROM pending_actions WHERE user_id = %s AND source_ref LIKE '%%:qa_thread_%%'",
                (user_id,),
            )
            counts["pending_actions"] += cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: pending_actions delete failed: %s", repr(e))

        if item_ids:
            # Delete item_classifications
            try:
                cursor.execute(
                    "DELETE FROM item_classifications WHERE user_id = %s AND scanned_item_id = ANY(%s)",
                    (user_id, item_ids),
                )
                counts["item_classifications"] = cursor.rowcount
            except Exception as e:
                logger.error("QA cleanup: item_classifications delete failed: %s", repr(e))

            # Delete detected_actions
            try:
                cursor.execute(
                    "DELETE FROM detected_actions WHERE user_id = %s AND scanned_item_id = ANY(%s)",
                    (user_id, item_ids),
                )
                counts["detected_actions"] = cursor.rowcount
            except Exception as e:
                logger.error("QA cleanup: detected_actions delete failed: %s", repr(e))

        # Delete the scanned_items themselves (must come before scanner_runs —
        # scanned_items.scanner_run_id has a FK to scanner_runs)
        try:
            cursor.execute(
                "DELETE FROM scanned_items WHERE user_id = %s AND source = 'qa_inject'",
                (user_id,),
            )
            counts["scanned_items"] = cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: scanned_items delete failed: %s", repr(e))

        # Delete scanner_runs created for qa_inject (after scanned_items)
        try:
            cursor.execute(
                "DELETE FROM scanner_runs WHERE user_id = %s AND source = 'qa_inject'",
                (user_id,),
            )
            counts["scanner_runs"] = cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: scanner_runs delete failed: %s", repr(e))

        # Delete qa_inject nudges directly inserted into nudges table
        try:
            cursor.execute(
                "DELETE FROM nudges WHERE user_id = %s AND nudge_type = 'qa_inject'",
                (user_id,),
            )
            counts["qa_nudges"] = cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: qa_inject nudges delete failed: %s", repr(e))

        # Delete calendar_event_nudges test rows and any nudge rows created by processor
        try:
            # Collect nudge_ids created when processor ran on our test rows
            cursor.execute(
                "SELECT nudge_id FROM calendar_event_nudges "
                "WHERE user_id = %s AND event_id LIKE 'qa_calendar_test_%%' AND nudge_id IS NOT NULL",
                (user_id,),
            )
            cal_nudge_ids = [r['nudge_id'] for r in cursor.fetchall()]

            # Delete the test rows
            cursor.execute(
                "DELETE FROM calendar_event_nudges WHERE user_id = %s AND event_id LIKE 'qa_calendar_test_%%'",
                (user_id,),
            )
            counts["calendar_event_nudges"] = cursor.rowcount

            # Delete nudge rows created by the processor for those test rows
            if cal_nudge_ids:
                cursor.execute(
                    "DELETE FROM nudges WHERE id = ANY(%s)",
                    (cal_nudge_ids,),
                )
                counts["calendar_derived_nudges"] = cursor.rowcount
            else:
                counts["calendar_derived_nudges"] = 0
        except Exception as e:
            logger.error("QA cleanup: calendar_event_nudges delete failed: %s", repr(e))

        # Delete qa_inject user_feedback rows
        try:
            cursor.execute(
                "DELETE FROM user_feedback WHERE user_id = %s "
                "AND feedback_context LIKE '%%qa_inject%%'",
                (user_id,),
            )
            counts["qa_feedback"] = cursor.rowcount
        except Exception as e:
            logger.error("QA cleanup: user_feedback delete failed: %s", repr(e))

        conn.commit()

    return {"deleted": counts}


# ---------------------------------------------------------------------------
# Baseline audit endpoint
# ---------------------------------------------------------------------------

@router.get("/baseline")
async def get_baseline(user_id: int = Depends(require_auth)):
    """
    Return raw counts for 10 key tables + 5 pipeline health metrics.
    Each table section is wrapped in try/except — a missing table returns 0/null.
    """
    result: dict = {}

    with get_db() as conn:
        cur = conn.cursor()

        # --- scanned_items ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM scanned_items WHERE user_id = %s", (user_id,))
            total_scanned = cur.fetchone()["n"]

            cur.execute(
                "SELECT source, COUNT(*) AS n FROM scanned_items WHERE user_id = %s GROUP BY source",
                (user_id,),
            )
            by_source = {r["source"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT COUNT(*) AS n FROM scanned_items WHERE user_id = %s AND processed = 0",
                (user_id,),
            )
            unprocessed = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM scanned_items WHERE user_id = %s AND source = 'qa_inject'",
                (user_id,),
            )
            qa_injected = cur.fetchone()["n"]

            result["scanned_items"] = {
                "total": total_scanned,
                "by_source": by_source,
                "unprocessed": unprocessed,
                "qa_injected": qa_injected,
            }
        except Exception as e:
            logger.error("baseline: scanned_items failed: %s", repr(e))
            result["scanned_items"] = {"total": 0, "by_source": {}, "unprocessed": 0, "qa_injected": 0}

        # --- item_classifications ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM item_classifications WHERE user_id = %s", (user_id,))
            total_ic = cur.fetchone()["n"]

            cur.execute(
                "SELECT relevance, COUNT(*) AS n FROM item_classifications WHERE user_id = %s GROUP BY relevance",
                (user_id,),
            )
            by_relevance = {r["relevance"]: r["n"] for r in cur.fetchall()}

            result["item_classifications"] = {"total": total_ic, "by_relevance": by_relevance}
        except Exception as e:
            logger.error("baseline: item_classifications failed: %s", repr(e))
            result["item_classifications"] = {"total": 0, "by_relevance": {}}

        # --- detected_actions ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM detected_actions WHERE user_id = %s", (user_id,))
            total_da = cur.fetchone()["n"]

            cur.execute(
                "SELECT status, COUNT(*) AS n FROM detected_actions WHERE user_id = %s GROUP BY status",
                (user_id,),
            )
            by_status_da = {r["status"]: r["n"] for r in cur.fetchall()}

            result["detected_actions"] = {"total": total_da, "by_status": by_status_da}
        except Exception as e:
            logger.error("baseline: detected_actions failed: %s", repr(e))
            result["detected_actions"] = {"total": 0, "by_status": {}}

        # --- nudges ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM nudges WHERE user_id = %s", (user_id,))
            total_nudges = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM nudges WHERE user_id = %s AND created_at > NOW() - INTERVAL '30 days'",
                (user_id,),
            )
            nudges_30d = cur.fetchone()["n"]

            cur.execute(
                "SELECT status, COUNT(*) AS n FROM nudges WHERE user_id = %s GROUP BY status",
                (user_id,),
            )
            by_status_n = {r["status"]: r["n"] for r in cur.fetchall()}

            result["nudges"] = {
                "total": total_nudges,
                "last_30_days": nudges_30d,
                "by_status": by_status_n,
            }
        except Exception as e:
            logger.error("baseline: nudges failed: %s", repr(e))
            result["nudges"] = {"total": 0, "last_30_days": 0, "by_status": {}}

        # --- pending_actions ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM pending_actions WHERE user_id = %s", (user_id,))
            total_pa = cur.fetchone()["n"]

            cur.execute(
                "SELECT status, COUNT(*) AS n FROM pending_actions WHERE user_id = %s GROUP BY status",
                (user_id,),
            )
            by_status_pa = {r["status"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT action_type, COUNT(*) AS n FROM pending_actions WHERE user_id = %s GROUP BY action_type",
                (user_id,),
            )
            by_type_pa = {r["action_type"]: r["n"] for r in cur.fetchall()}

            result["pending_actions"] = {
                "total": total_pa,
                "by_status": by_status_pa,
                "by_type": by_type_pa,
            }
        except Exception as e:
            logger.error("baseline: pending_actions failed: %s", repr(e))
            result["pending_actions"] = {"total": 0, "by_status": {}, "by_type": {}}

        # --- research_audit_runs ---
        try:
            cur.execute("SELECT COUNT(*) AS n, MAX(run_at) AS last_run FROM research_audit_runs WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            result["research_audit_runs"] = {
                "total": row["n"],
                "last_run_at": str(row["last_run"]) if row["last_run"] else None,
            }
        except Exception as e:
            logger.error("baseline: research_audit_runs failed: %s", repr(e))
            result["research_audit_runs"] = {"total": 0, "last_run_at": None}

        # --- entity_mappings ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM entity_mappings WHERE user_id = %s", (user_id,))
            total_em = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM entity_mappings WHERE user_id = %s AND person_id IS NOT NULL",
                (user_id,),
            )
            resolved = cur.fetchone()["n"]

            result["entity_mappings"] = {
                "total": total_em,
                "resolved": resolved,
                "unresolved": total_em - resolved,
            }
        except Exception as e:
            logger.error("baseline: entity_mappings failed: %s", repr(e))
            result["entity_mappings"] = {"total": 0, "resolved": 0, "unresolved": 0}

        # --- user_pattern_preferences ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM user_pattern_preferences WHERE user_id = %s", (user_id,))
            result["user_pattern_preferences"] = {"total": cur.fetchone()["n"]}
        except Exception as e:
            logger.error("baseline: user_pattern_preferences failed: %s", repr(e))
            result["user_pattern_preferences"] = {"total": 0}

        # --- lcd_observation_log ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM lcd_observation_log WHERE user_id = %s", (user_id,))
            result["lcd_observation_log"] = {"total": cur.fetchone()["n"]}
        except Exception as e:
            logger.error("baseline: lcd_observation_log failed: %s", repr(e))
            result["lcd_observation_log"] = {"total": 0}

        # --- activity_log ---
        try:
            cur.execute("SELECT COUNT(*) AS n FROM activity_log WHERE user_id = %s", (user_id,))
            result["activity_log"] = {"total": cur.fetchone()["n"]}
        except Exception as e:
            logger.error("baseline: activity_log failed: %s", repr(e))
            result["activity_log"] = {"total": 0}

        # --- health metrics ---
        health: dict = {}

        try:
            cur.execute(
                "SELECT COUNT(*) AS n FROM scanned_items WHERE user_id = %s AND processed = 0 AND detected_at < NOW() - INTERVAL '1 hour'",
                (user_id,),
            )
            health["unclassified_backlog_count"] = cur.fetchone()["n"]
        except Exception as e:
            logger.error("baseline: health.unclassified_backlog_count failed: %s", repr(e))
            health["unclassified_backlog_count"] = 0

        try:
            total_s = result.get("scanned_items", {}).get("total", 0)
            total_c = result.get("item_classifications", {}).get("total", 0)
            health["classification_coverage_pct"] = (
                100.0 if total_s == 0 else round((total_c / total_s) * 100, 1)
            )
        except Exception as e:
            logger.error("baseline: health.classification_coverage_pct failed: %s", repr(e))
            health["classification_coverage_pct"] = None

        try:
            cur.execute(
                "SELECT COUNT(*) AS n FROM pending_actions WHERE user_id = %s AND action_type = 'calendar_proposal' AND created_at > NOW() - INTERVAL '7 days'",
                (user_id,),
            )
            health["scheduling_proposals_7d"] = cur.fetchone()["n"]
        except Exception as e:
            logger.error("baseline: health.scheduling_proposals_7d failed: %s", repr(e))
            health["scheduling_proposals_7d"] = 0

        try:
            cur.execute(
                "SELECT MAX(sent_at) AS last_sent FROM nudges WHERE user_id = %s AND status IN ('sent', 'delivered')",
                (user_id,),
            )
            row = cur.fetchone()
            health["last_nudge_sent_at"] = str(row["last_sent"]) if row and row["last_sent"] else None
        except Exception as e:
            logger.error("baseline: health.last_nudge_sent_at failed: %s", repr(e))
            health["last_nudge_sent_at"] = None

        try:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(detected_at)))/3600 AS age_hours FROM scanned_items WHERE user_id = %s AND processed = 0",
                (user_id,),
            )
            row = cur.fetchone()
            health["oldest_unprocessed_item_age_hours"] = (
                round(float(row["age_hours"]), 2) if row and row["age_hours"] is not None else None
            )
        except Exception as e:
            logger.error("baseline: health.oldest_unprocessed_item_age_hours failed: %s", repr(e))
            health["oldest_unprocessed_item_age_hours"] = None

        result["health"] = health

    return result


@router.get("/systems-health")
async def systems_health(_: int = Depends(require_auth)):
    """Return heartbeat status for all monitored background subsystems."""
    from web.core.database import get_system_health

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

    rows = get_system_health()
    now = datetime.utcnow()
    results = []

    # Include all known subsystems, even ones not yet in DB (never ran)
    seen = {r["subsystem"] for r in rows}
    for name, threshold in THRESHOLDS.items():
        if name not in seen:
            rows.append({"subsystem": name, "last_run_at": None, "last_error": None})

    for row in rows:
        name = row["subsystem"]
        threshold = THRESHOLDS.get(name, 60)
        last_run = row.get("last_run_at")

        if last_run is None:
            status = "red"
            minutes_ago = None
        else:
            if isinstance(last_run, str):
                last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00")).replace(tzinfo=None)
            else:
                last_run_dt = last_run
            minutes_ago = (now - last_run_dt).total_seconds() / 60
            if minutes_ago <= threshold:
                status = "green"
            elif minutes_ago <= threshold * 1.5:
                status = "yellow"
            else:
                status = "red"

        results.append({
            "subsystem": name,
            "status": status,
            "last_run_at": row.get("last_run_at"),
            "minutes_ago": round(minutes_ago, 1) if minutes_ago is not None else None,
            "threshold_minutes": threshold,
            "last_error": row.get("last_error"),
        })

    results.sort(key=lambda x: x["subsystem"])
    return {"subsystems": results, "checked_at": now.isoformat()}
