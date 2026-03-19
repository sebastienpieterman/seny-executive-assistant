"""
Screen agent API endpoints for Seny.

Provides endpoints for the external desktop screen awareness agent:
- GET /api/screen/priority: Returns active priority items
- POST /api/screen/evaluate: Accepts screenshot, calls Claude Vision, fires nudge if drifting

Screenshots are NEVER stored in the database — evaluated in memory and discarded immediately.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from anthropic import AsyncAnthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from collections import Counter

from web.auth.jwt_utils import require_screen_agent
from web.core.database import (
    get_priority_items, get_screen_dismissal_patterns, get_telegram_bot_user_links_for_user,
    get_screen_cooldown_until, set_screen_cooldown_until,
    get_screen_last_nudge_at, set_screen_last_nudge_at, clear_screen_last_nudge_at,
    add_screen_nudge_message, get_screen_nudge_message_user,
    append_lcd_observation, get_db,
)
from web.services.telegram_bot_service import TelegramBotService
from src.core.config import Config

router = APIRouter(prefix="/api/screen", tags=["screen"])
_logger = logging.getLogger("screen_agent")

# Server-side cooldown durations (state stored in DB, shared across workers)
_COOLDOWN_SECONDS = 1200  # 20 minutes after a nudge fires
_DISMISS_COOLDOWN_SECONDS = 7200  # 2 hours when user says "I'm working"

# How long after a screen nudge a plain (non-reply) message is still treated as a response
_SCREEN_NUDGE_REPLY_WINDOW = 300  # 5 minutes


def is_screen_nudge_message(telegram_message_id: int) -> Optional[str]:
    """Check if a Telegram message_id was sent by the screen agent. Returns user_id or None."""
    return get_screen_nudge_message_user(telegram_message_id)


def has_recent_screen_nudge(user_id: str) -> bool:
    """Check if a screen nudge was sent to this user within the reply window (5 min)."""
    last_sent = get_screen_last_nudge_at(user_id)
    if not last_sent:
        return False
    return (time.time() - last_sent) < _SCREEN_NUDGE_REPLY_WINDOW


def dismiss_screen_nudge(user_id: str) -> None:
    """User said they're working — extend cooldown to 2 hours."""
    set_screen_cooldown_until(user_id, time.time() + _DISMISS_COOLDOWN_SECONDS)
    clear_screen_last_nudge_at(user_id)


_SHORT_COOLDOWN_SECONDS = 600  # 10 minutes — used when Claude pushes back


def set_short_cooldown(user_id: str) -> None:
    """Set a short 10-minute cooldown (used when Claude pushes back on a dismissal)."""
    set_screen_cooldown_until(user_id, time.time() + _SHORT_COOLDOWN_SECONDS)
    clear_screen_last_nudge_at(user_id)


# ---------------------------------------------------------------------------
# GET /api/screen/priority
# ---------------------------------------------------------------------------

@router.get("/priority")
async def get_screen_priority(user_id: str = Depends(require_screen_agent)):
    """Return active priority context items for the screen agent."""
    items = get_priority_items(int(user_id), status="active")
    return {"items": [
        {
            "id": item["id"],
            "title": item["title"],
            "item_type": item["item_type"],
            "priority_level": item["priority_level"],
            "due_at": item.get("due_at"),
        }
        for item in (items or [])
    ]}


# ---------------------------------------------------------------------------
# POST /api/screen/evaluate
# ---------------------------------------------------------------------------

class ScreenEvalRequest(BaseModel):
    screenshot_b64: str        # JPEG base64-encoded screenshot
    machine_id: str            # socket.gethostname() from agent
    escalation_stage: int = 0  # 0=none, 1=first, 2=second, 3=third


class ScreenEvalResponse(BaseModel):
    status: str                # "on_track", "drifting"
    nudge_fired: bool
    message: Optional[str] = None


@router.post("/evaluate", response_model=ScreenEvalResponse)
async def evaluate_screen(
    req: ScreenEvalRequest,
    user_id: str = Depends(require_screen_agent),
):
    """
    Accept a screenshot from the screen agent, evaluate with Claude Vision,
    and send a direct Telegram message if the user is drifting.

    Screenshots are NEVER stored — evaluated in memory and discarded immediately.
    Fails open: on any error returns on_track/False to avoid disrupting the user.
    """
    try:
        try:
            from zoneinfo import ZoneInfo
            from web.core.database import get_user_settings as _get_user_settings
            _settings = _get_user_settings(int(user_id))
            _tz_name = (_settings.get('digest_timezone', 'America/Chicago') if _settings else 'America/Chicago')
            _tz = ZoneInfo(_tz_name)
        except Exception:
            from zoneinfo import ZoneInfo
            _tz = ZoneInfo('America/Chicago')
        today = datetime.now(timezone.utc).astimezone(_tz).strftime("%Y-%m-%d %A %I:%M %p")

        # Fetch learned dismissal patterns (accepted + pushback)
        learned_patterns_block = ""
        try:
            dismissals = get_screen_dismissal_patterns(int(user_id), days=30)

            # Accepted: "this counts as working"
            accepted_reasons = [
                d['user_reason'] for d in dismissals
                if d.get('accepted') and d.get('user_reason')
            ]

            # Pushback: "user didn't want interruption even though off-task"
            pushback_reasons = [
                d['user_reason'] for d in dismissals
                if not d.get('accepted') and d.get('user_reason')
            ]

            if accepted_reasons:
                reason_counts = Counter(r.lower().strip() for r in accepted_reasons)
                frequent = [
                    (reason, count)
                    for reason, count in reason_counts.most_common(5)
                    if count >= 2
                ]
                if frequent:
                    pattern_lines = [f'- "{reason}" ({count} times)' for reason, count in frequent]
                    learned_patterns_block += (
                        "\n\nThe user has previously explained these activities as productive:\n"
                        + "\n".join(pattern_lines)
                        + "\nIf you see something matching a known-productive pattern, return on_track."
                    )

            if pushback_reasons:
                reason_counts = Counter(r.lower().strip() for r in pushback_reasons)
                frequent = [
                    (reason, count)
                    for reason, count in reason_counts.most_common(3)
                    if count >= 2
                ]
                if frequent:
                    pattern_lines = [f'- "{reason}" ({count} times)' for reason, count in frequent]
                    learned_patterns_block += (
                        "\n\nThe user has previously asked NOT to be interrupted during:\n"
                        + "\n".join(pattern_lines)
                        + "\nIf current activity matches a known-pushback pattern, return off_track "
                        + "but set a shorter cooldown (the user knows they're off-task and will self-correct)."
                    )
        except Exception as e:
            _logger.warning(f"[screen_agent] Failed to fetch dismissal patterns: {repr(e)}")

        prompt = f"""Today is {today}.

Look at this screenshot. Evaluate:
1. Is this person doing focused work, or are they consuming entertainment or social media?

Respond with JSON only, no other text:
{{"status": "on_track" | "drifting", "activity": "brief description of what the user is doing"}}

Rules:
- "on_track": user is doing focused work — coding, writing, reading work documents, emails, spreadsheets, design tools, etc.
- "drifting": user is watching YouTube, scrolling social media (Twitter/X, Reddit, Instagram, TikTok, Facebook), browsing entertainment sites, playing games, or passively consuming content. YouTube IS drifting unless it's clearly a technical tutorial alongside active work.
- "activity": one short phrase describing what you see (e.g. "watching YouTube", "scrolling Twitter", "browsing Reddit"). Required for drifting status. For on_track, omit or leave blank.
- When genuinely uncertain (e.g. a work-related article), return on_track. But entertainment and social media are NOT uncertain — they are drifting.{learned_patterns_block}"""

        # Skip Vision call if drip nudge fired recently (cross-agent cooldown )
        try:
            from web.core.database import get_db as _get_db
            with _get_db() as _conn:
                _cur = _conn.cursor()
                _cur.execute(
                    "SELECT id FROM nudges"
                    " WHERE user_id = %s AND nudge_type != 'screen_agent'"
                    " AND status IN ('sent', 'delivered')"
                    " AND sent_at > NOW() - INTERVAL '5 minutes'"
                    " LIMIT 1",
                    (int(user_id),)
                )
                if _cur.fetchone():
                    _logger.info(
                        "[screen_agent] Drip nudge fired recently for user_id=%s, skipping Vision call",
                        user_id
                    )
                    return ScreenEvalResponse(status="on_track", nudge_fired=False, message=None)
        except Exception as _cooldown_err:
            _logger.warning("[screen_agent] Cross-agent cooldown check failed (proceeding): %s", repr(_cooldown_err))

        # Skip Vision call when user is in a declared focus state
        try:
            from web.core.database import get_user_status as _get_user_status
            _active_status = _get_user_status(int(user_id))
            if _active_status:
                _logger.info(
                    "[screen_agent] User status active for user_id=%s (%s), skipping Vision call",
                    user_id, _active_status.get('status_text', '')
                )
                return ScreenEvalResponse(status="on_track", nudge_fired=False, message=None)
        except Exception as _status_err:
            _logger.warning("[screen_agent] user_status check failed (proceeding): %s", repr(_status_err))

        # Call Claude Haiku with base64 image block
        client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": req.screenshot_b64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        # Parse JSON response — fail open on any parse error
        raw_text = response.content[0].text.strip()
        # Strip markdown code fence lines (handles leading, trailing, or both)
        raw_text = "\n".join(
            line for line in raw_text.splitlines() if not line.startswith("```")
        ).strip()
        try:
            result = json.loads(raw_text)
            eval_status = result.get("status", "on_track")
            activity = result.get("activity", "")
            _logger.info(f"[screen_agent] Vision result for user_id={user_id}: status={eval_status}, activity={activity!r}")
        except Exception:
            _logger.warning(f"[screen_agent] Failed to parse Vision response for user_id={user_id}: {raw_text!r}")
            return ScreenEvalResponse(status="on_track", nudge_fired=False)

        # Validate status value
        if eval_status not in ("on_track", "drifting"):
            eval_status = "on_track"

        # Fire message if drifting
        nudge_fired = False
        nudge_message = None

        if eval_status == "drifting":
            # Check quiet hours (uses user's nudge quiet hours setting)
            from web.services.nudge_service import NudgeService
            if NudgeService(int(user_id)).is_quiet_hours():
                _logger.info(f"[screen_agent] Quiet hours active for user_id={user_id}, skipping message")
                return ScreenEvalResponse(status=eval_status, nudge_fired=False, message=None)

            # Check cooldown
            now = time.time()
            if now < get_screen_cooldown_until(user_id):
                _logger.info(f"[screen_agent] Cooldown active for user_id={user_id}, skipping message")
                return ScreenEvalResponse(
                    status=eval_status,
                    nudge_fired=False,
                    message=None,
                )

            stage = req.escalation_stage

            # Determine message based on escalation stage
            if stage <= 1:
                nudge_message = "Hey — should you be working right now?"
            elif stage == 2:
                nudge_message = "Still drifting. Time to refocus."
            else:
                nudge_message = "You've been off-task for a while now. Get back to it."

            # Send via unified nudge delivery path
            try:
                from web.services.nudge_service import NudgeService
                svc = NudgeService(int(user_id))
                send_result = await svc.send_nudge(
                    nudge_type='screen_agent',
                    title='Focus check',
                    body=nudge_message,
                    urgency='urgent',
                    source_type='screen_agent',
                    source_id=None,
                )
                if send_result.get('success'):
                    nudge_fired = True
                    set_screen_cooldown_until(user_id, now + _COOLDOWN_SECONDS)
                    # Track telegram_message_id for reply detection
                    if send_result.get('telegram_message_id'):
                        add_screen_nudge_message(send_result['telegram_message_id'], user_id)
                    set_screen_last_nudge_at(user_id, now)
                    # Write LCD observation (rate-limited: max 1 per 4 hours)
                    try:
                        with get_db() as _conn:
                            _cur = _conn.cursor()
                            _cur.execute(
                                "SELECT id FROM lcd_observation_log WHERE user_id=%s AND source='screen_agent'"
                                " AND created_at > NOW() - INTERVAL '4 hours' LIMIT 1",
                                (int(user_id),)
                            )
                            _recent = _cur.fetchone()
                        if not _recent:
                            _time_str = datetime.now(timezone.utc).astimezone(_tz).strftime('%I:%M %p')
                            _activity = activity.strip() if activity else ""
                            _obs_text = f"Drifting at {_time_str} — {_activity}" if _activity else f"Drifting at {_time_str} — off-task"
                            append_lcd_observation(int(user_id), source='screen_agent', content=_obs_text)
                            _logger.info("[screen_agent] LCD observation written for user_id=%s: %r", user_id, _obs_text)
                        else:
                            _logger.info("[screen_agent] LCD rate limit active for user_id=%s, skipping observation", user_id)
                    except Exception as _lcd_err:
                        _logger.warning("[screen_agent] LCD write failed (non-blocking): %s", repr(_lcd_err))
            except Exception as e:
                _logger.warning(f"[screen_agent] Nudge delivery failed for user_id={user_id}: {repr(e)}")

        return ScreenEvalResponse(
            status=eval_status,
            nudge_fired=nudge_fired,
            message=nudge_message,
        )

    except Exception as e:
        _logger.warning(f"[screen_agent] evaluate_screen error for user_id={user_id}: {repr(e)}")
        return ScreenEvalResponse(status="on_track", nudge_fired=False)
