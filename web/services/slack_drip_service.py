"""
Slack Drip Scanner Service
==========================

Replaces the APScheduler-based Slack batch scanner with a continuous asyncio
background loop that scans ONE channel every 10 seconds in round-robin order.

Architecture:
- One iteration per 10 seconds, scanning EXACTLY ONE channel per tick
- Rotates through all (user, workspace) pairs in round-robin order
- Always picks the most stale non-excluded channel within the selected workspace
- Per-channel circuit breaker persisted to slack_channel_cursors (survives restarts)
- Rate: ~6 API calls/minute total across all workspaces = 12% of Slack Tier 3 (50/min)

Why this exists:
- Old batch scanner hammered all channels simultaneously (~60+ calls in 60s)
- Exceeded Slack's rate limit → continuous 429 responses all day
- Circuit breaker was in-memory → lost on every Railway deploy → immediately hammered again
- Drip model is self-regulating: slow, steady, never bursts, always fresh

Tuning:
- DRIP_INTERVAL_SECONDS: seconds between each channel scan (start at 10)
- CIRCUIT_RECOVERY_SECONDS: seconds before retrying a failed channel (default 900 = 15min)
- CHANNEL_REFRESH_HOURS: how often to re-fetch the channel list from Slack API
- MAX_MESSAGES_PER_DRIP: messages fetched per channel per tick (default 50)

See .planning/codebase/slack-drip-tracker.md for adjustment history.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from web.core.database import (
    get_db,
    get_next_slack_channel_to_scan,
    get_slack_channel_cursor,
    insert_scanned_item,
    upsert_slack_channel_cursor,
    get_channel_exclusion_preferences,
)

logger = logging.getLogger(__name__)

# ============================================================
# Configuration — adjust these to tune scan frequency
# ============================================================

DRIP_INTERVAL_SECONDS: float = 15.0       # Seconds between each channel scan tick
CIRCUIT_RECOVERY_SECONDS: int = 900       # 15 minutes before retrying a failed channel
CHANNEL_REFRESH_HOURS: int = 4            # Refresh channel list every N hours
MAX_MESSAGES_PER_DRIP: int = 50           # conversations.history limit per tick
CIRCUIT_FAILURE_THRESHOLD: int = 3        # Failures before opening circuit for a channel

# ============================================================
# Module-level state
# ============================================================

_drip_task: Optional[asyncio.Task] = None
_running: bool = False
_workspace_index: int = 0  # Round-robin pointer across all (user, workspace) pairs


# ============================================================
# Public interface
# ============================================================

async def start_drip_loop():
    """Start the Slack drip scanner as a background asyncio task.

    Called once from web/main.py on app startup. Safe to call multiple times —
    if the loop is already running, logs a warning and returns.
    """
    global _drip_task, _running

    if _running:
        logger.warning("[SlackDrip] start_drip_loop() called but drip is already running")
        return

    _running = True
    _drip_task = asyncio.create_task(_drip_loop(), name="slack_drip_scanner")
    print("✓ Slack drip scanner started (one channel every 10s)", flush=True)
    logger.info("[SlackDrip] Drip loop task created")


async def stop_drip_loop():
    """Gracefully stop the drip loop. Called on app shutdown."""
    global _drip_task, _running

    _running = False
    if _drip_task and not _drip_task.done():
        _drip_task.cancel()
        try:
            await _drip_task
        except asyncio.CancelledError:
            pass
    logger.info("[SlackDrip] Drip loop stopped")


# ============================================================
# Core drip loop
# ============================================================

async def _drip_loop():
    """The main drip loop. Runs forever until _running is False or task is cancelled.

    Each iteration:
    1. Loads all users
    2. For each user, for each Slack workspace: refresh channels if stale
    3. Picks the most stale non-excluded channel
    4. Checks circuit breaker for that channel
    5. Scans the channel (calls conversations.history)
    6. Updates cursor with new timestamp
    7. Waits DRIP_INTERVAL_SECONDS
    """
    # Track when we last refreshed channel lists (per user+team)
    last_channel_refresh: dict[str, float] = {}  # "{user_id}:{team_id}" → unix timestamp

    logger.info("[SlackDrip] Loop starting")

    tick = 0
    while _running:
        tick += 1
        print(f"[SlackDrip] Tick #{tick} starting", flush=True)
        try:
            await _drip_tick(last_channel_refresh)
        except asyncio.CancelledError:
            raise  # Don't swallow cancellation
        except Exception as e:
            # Top-level guard: log and continue. Never let a crash kill the loop.
            logger.error("[SlackDrip] Unexpected error in drip tick: %r", e)

        print(f"[SlackDrip] Tick #{tick} done", flush=True)
        await asyncio.sleep(DRIP_INTERVAL_SECONDS)

    logger.info("[SlackDrip] Loop exited cleanly")


async def _drip_tick(last_channel_refresh: dict):
    """One iteration of the drip loop.

    Scans exactly ONE channel per tick, rotating through all (user, workspace)
    pairs in round-robin order. This ensures the rate stays at one
    conversations.history call per DRIP_INTERVAL_SECONDS regardless of how
    many workspaces are connected.
    """
    global _workspace_index
    from web.services.slack_service import SlackService

    # Build the current list of all connected (user_id, team_id) pairs
    workspace_list = []
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")

            users = cursor.fetchall()
    except Exception as e:
        logger.error("[SlackDrip] Failed to fetch users: %r", e)
        return

    for user_row in users:
        user_id = user_row[0] if isinstance(user_row, tuple) else user_row['id']
        try:
            workspaces = SlackService.list_connected_workspaces(user_id)
            for workspace in workspaces:
                team_id = workspace.get('team_id')
                if team_id:
                    workspace_list.append((user_id, team_id))
        except Exception as e:
            logger.debug("[SlackDrip] Failed to list workspaces for user %d: %r", user_id, e)

    if not workspace_list:
        logger.info("[SlackDrip] No connected workspaces found — drip idle")
        return

    # Advance the round-robin pointer and pick one workspace for this tick
    _workspace_index = _workspace_index % len(workspace_list)
    user_id, team_id = workspace_list[_workspace_index]
    _workspace_index += 1

    try:
        await _drip_workspace(user_id, team_id, last_channel_refresh)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("[SlackDrip] User %d workspace %s error: %r", user_id, team_id, e)


async def _drip_workspace(user_id: int, team_id: str, last_channel_refresh: dict):
    """Handle one drip tick for a specific user+workspace."""
    from web.services.slack_service import SlackService

    slack = SlackService(user_id, team_id)
    if not slack.is_connected():
        return

    # Check workspace circuit breaker before attempting any API calls.
    # If open, skip this workspace silently — avoids per-tick log spam.
    # The circuit auto-recovers after CIRCUIT_RECOVERY_SECONDS (15 min).
    from web.services.slack_service import SlackCircuitOpenError
    try:
        slack._check_circuit()
    except SlackCircuitOpenError as e:
        logger.debug("[SlackDrip] User %d workspace %s: circuit open, skipping (%s)", user_id, team_id, e)
        return

    # Refresh channel list if stale (every CHANNEL_REFRESH_HOURS)
    now = time.time()
    refresh_key = f"{user_id}:{team_id}"
    last_refresh = last_channel_refresh.get(refresh_key, 0)
    if (now - last_refresh) > (CHANNEL_REFRESH_HOURS * 3600):
        await _refresh_channel_list(user_id, team_id, slack)
        last_channel_refresh[refresh_key] = now

    # Pick the most stale channel
    channel = get_next_slack_channel_to_scan(user_id, team_id)
    if not channel:
        logger.debug("[SlackDrip] User %d workspace %s: no channels to scan", user_id, team_id)
        return

    # Check circuit breaker for this channel
    if not _check_channel_circuit(channel, user_id, team_id):
        logger.debug(
            "[SlackDrip] User %d channel %s: circuit OPEN, skipping",
            user_id, channel['channel_id']
        )
        return

    # Scan the channel
    await _scan_single_channel(user_id, team_id, channel, slack)


async def _refresh_channel_list(user_id: int, team_id: str, slack) -> None:
    """Fetch all channels for this workspace and register them in slack_channel_cursors.

    Also refreshes the exclusion status for each channel from user_settings.
    Called every CHANNEL_REFRESH_HOURS to pick up new channels or exclusion changes.

    Does NOT delete channels that have been removed from the workspace — they just
    stop being picked for scanning (last_scan_at won't update, but that's fine).
    """
    logger.debug("[SlackDrip] Refreshing channel list for user %d workspace %s", user_id, team_id)

    try:
        # Get exclusion preferences
        exclusions = get_channel_exclusion_preferences(user_id)
        excluded_ids = set(exclusions.get('slack_excluded_channels', []))

        # Fetch channels (public + private)
        channels = await slack.list_channels(types="public_channel,private_channel", limit=200) or []
        # Fetch DMs
        dms = await slack.list_dms(limit=100) or []
        # Fetch group DMs
        group_dms = await slack.list_group_dms(limit=100) or []

        all_convs = []
        for ch in channels:
            all_convs.append({
                'id': ch.get('id'),
                'name': ch.get('name', ''),
                'type': 'channel',
                'is_dm': False,
            })
        for dm in dms:
            # DMs return: {id, user_id, user_name}
            dm_name = dm.get('user_name') or dm.get('user_id', dm.get('id', 'DM'))
            all_convs.append({
                'id': dm.get('id'),
                'name': dm_name,
                'type': 'im',
                'is_dm': True,
            })
        for gdm in group_dms:
            # Group DMs return: {id, name, num_members}
            all_convs.append({
                'id': gdm.get('id'),
                'name': gdm.get('name', 'GroupDM'),
                'type': 'mpim',
                'is_dm': True,
            })

        for conv in all_convs:
            channel_id = conv.get('id')
            if not channel_id:
                continue
            is_excluded = channel_id in excluded_ids
            upsert_slack_channel_cursor(
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                channel_name=conv.get('name'),
                channel_type=conv.get('type', 'channel'),
                is_excluded=is_excluded,
            )

        logger.info(
            "[SlackDrip] User %d workspace %s: registered %d channels (%d excluded)",
            user_id, team_id, len(all_convs), len(excluded_ids)
        )

    except Exception as e:
        logger.error("[SlackDrip] Channel refresh failed for user %d workspace %s: %r", user_id, team_id, e)


def _check_channel_circuit(channel: dict, user_id: int, team_id: str) -> bool:
    """Check if the per-channel circuit breaker allows scanning.

    Returns True if the channel should be scanned, False if circuit is open.
    Handles the open → half_open transition based on CIRCUIT_RECOVERY_SECONDS.
    """
    circuit_state = channel.get('circuit_state', 'closed')

    if circuit_state == 'closed':
        return True  # Normal operation

    if circuit_state == 'half_open':
        return True  # Allow one probe attempt

    if circuit_state == 'open':
        opened_at_str = channel.get('circuit_opened_at')
        if not opened_at_str:
            # No timestamp — treat as ready to retry
            upsert_slack_channel_cursor(
                user_id=user_id,
                team_id=team_id,
                channel_id=channel['channel_id'],
                circuit_state='half_open',
            )
            return True

        try:
            opened_at = datetime.fromisoformat(opened_at_str.replace('Z', '+00:00'))
            elapsed = (datetime.now(timezone.utc) - opened_at).total_seconds()
            if elapsed >= CIRCUIT_RECOVERY_SECONDS:
                # Recovery time elapsed — transition to half_open
                upsert_slack_channel_cursor(
                    user_id=user_id,
                    team_id=team_id,
                    channel_id=channel['channel_id'],
                    circuit_state='half_open',
                )
                logger.info(
                    "[SlackDrip] Channel %s circuit half-open after %.0fs",
                    channel['channel_id'], elapsed
                )
                return True
            else:
                return False  # Still in recovery window
        except (ValueError, TypeError):
            # Can't parse timestamp — allow attempt
            return True

    return True  # Unknown state — allow attempt


async def _scan_single_channel(user_id: int, team_id: str, channel: dict, slack) -> None:
    """Scan a single Slack channel for new messages since last cursor position.

    Inserts new messages into scanned_items with scanner_run_id=None.
    Updates the channel cursor on success.
    Updates circuit breaker state on failure.
    """
    channel_id = channel['channel_id']
    channel_name = channel.get('channel_name', channel_id)
    last_scan_ts = channel.get('last_scan_ts')  # May be None for first scan
    is_dm = channel.get('channel_type') in ('im', 'mpim')

    try:
        # Fetch messages since last scan
        messages = await slack.get_messages(
            channel_id,
            limit=MAX_MESSAGES_PER_DRIP,
            oldest=last_scan_ts,
        )

        if not messages:
            # No new messages — update cursor timestamp to now so this channel
            # doesn't keep getting picked as most stale
            current_ts = str(datetime.now(timezone.utc).timestamp())
            upsert_slack_channel_cursor(
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                last_scan_ts=current_ts,
                consecutive_failures=0,
                circuit_state='closed',
            )
            return

        # Get authed_user_id for direction detection
        token_data = slack._load_token() or {}
        authed_user_id = token_data.get('authed_user_id', '')

        # Get user display name map
        try:
            user_map = await slack.get_users_map() or {}
        except Exception:
            user_map = {}

        items_new = 0
        newest_ts = last_scan_ts  # Track the newest message ts for cursor update

        for msg in messages:
            msg_ts = msg.get('ts', '')
            if not msg_ts:
                continue

            # Track newest timestamp for cursor
            if newest_ts is None or msg_ts > newest_ts:
                newest_ts = msg_ts

            msg_user_id = msg.get('user', '')
            username = user_map.get(msg_user_id, msg_user_id)

            # Direction: outbound if message is from the authenticated user
            direction = 'outbound' if (authed_user_id and msg_user_id == authed_user_id) else 'inbound'

            metadata = {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "user_id": msg_user_id,
                "username": username,
                "text": (msg.get('text', '') or '')[:500],
                "thread_ts": msg.get('thread_ts'),
                "is_dm": is_dm,
                "team_id": team_id,
            }

            # source_id = unique message identifier
            source_id = f"{channel_id}:{msg_ts}"

            result = insert_scanned_item(
                user_id=user_id,
                scanner_run_id=None,  # Drip mode — no discrete scan run
                source='slack',
                source_id=source_id,
                source_metadata=json.dumps(metadata),
                item_type='slack_message',
                direction=direction,
            )

            if result:
                items_new += 1

        # Update cursor to newest message timestamp
        if newest_ts:
            upsert_slack_channel_cursor(
                user_id=user_id,
                team_id=team_id,
                channel_id=channel_id,
                last_scan_ts=newest_ts,
                last_error='',  # Clear any previous error
                consecutive_failures=0,
                circuit_state='closed',
            )

        if items_new > 0:
            logger.info(
                "[SlackDrip] User %d channel %s (%s): %d new messages",
                user_id, channel_id, channel_name, items_new
            )

    except Exception as e:
        error_repr = repr(e)

        # Get current failure count
        current = get_slack_channel_cursor(user_id, team_id, channel_id)
        failures = (current.get('consecutive_failures', 0) if current else 0) + 1

        new_circuit_state = 'closed'
        circuit_opened_at = None

        if failures >= CIRCUIT_FAILURE_THRESHOLD:
            new_circuit_state = 'open'
            circuit_opened_at = datetime.now(timezone.utc).isoformat()
            logger.warning(
                "[SlackDrip] Channel %s circuit OPEN after %d failures. Last error: %s",
                channel_id, failures, error_repr
            )
        else:
            logger.warning(
                "[SlackDrip] Channel %s failure %d/%d: %s",
                channel_id, failures, CIRCUIT_FAILURE_THRESHOLD, error_repr
            )

        upsert_slack_channel_cursor(
            user_id=user_id,
            team_id=team_id,
            channel_id=channel_id,
            last_error=error_repr[:500],  # Truncate for DB
            consecutive_failures=failures,
            circuit_state=new_circuit_state,
            circuit_opened_at=circuit_opened_at,
        )
