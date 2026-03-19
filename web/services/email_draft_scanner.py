"""
Email Draft Scanner — Phase 45 (Email Drafting)

Background job that finds Gmail emails needing a reply (last 7 days) and
creates pending_action draft cards so they surface in the EA queue without
the user having to notice them manually.

Runs every 6 hours via APScheduler (registered in scheduler.py).
"""

import json
import logging
from datetime import datetime, timedelta

from web.core.database import (
    create_pending_action,
    get_needs_reply_items,
    get_pending_action_by_source_ref,
    list_google_tokens,
)

logger = logging.getLogger(__name__)


async def process_email_draft_proposals() -> None:
    """
    Scan all users' classified Gmail items for unhandled reply opportunities
    and create pending_action draft cards for each one.

    Deduplicates by source_ref (scanned_item_id) so each email only surfaces
    once regardless of how many times the job runs.
    """
    from web.core.database import get_db

    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM users")
            users = cursor.fetchall()
    except Exception as e:
        logger.error("Email draft scanner: failed to fetch users: %s", repr(e))
        return

    if not users:
        logger.debug("Email draft scanner: no users found")
        return

    since = (datetime.now() - timedelta(days=7)).isoformat()
    total_users = 0
    total_created = 0

    for row in users:
        user_id = row['id']
        try:
            # Skip users with no connected Gmail accounts
            tokens = list_google_tokens(user_id)
            if not tokens:
                continue

            total_users += 1

            # Fetch classified items from the last 7 days that need a reply
            items = get_needs_reply_items(user_id, since=since, limit=20)

            for item in items:
                # Only process Gmail items in
                if item.get('source') != 'gmail':
                    continue

                source_ref = str(item['scanned_item_id'])

                # Dedup: skip if a pending action already exists for this item
                if get_pending_action_by_source_ref(user_id, source_ref):
                    continue

                # Parse metadata stored by the scanner
                meta = json.loads(item.get('source_metadata') or '{}')
                to_email = meta.get('from', '')
                original_subject = meta.get('subject', '')
                gmail_account = meta.get('account', '')
                message_id = item.get('source_id', '')

                # Skip if we don't know who to reply to
                if not to_email:
                    continue

                reply_subject = (
                    original_subject
                    if original_subject.lower().startswith('re:')
                    else f"Re: {original_subject}"
                )

                content = {
                    "to": to_email,
                    "cc": None,
                    "subject": reply_subject,
                    "body": "",  # Intentionally empty — user must fill in before sending
                    "thread_id": message_id or None,
                    "gmail_account": gmail_account or None,
                }

                title = f"Reply to: {original_subject}"
                if len(title) > 120:
                    title = title[:117] + "..."

                action_id = create_pending_action(
                    user_id,
                    'email_draft',
                    title,
                    json.dumps(content),
                    'scanner',
                    source_ref,
                )

                if action_id:
                    total_created += 1
                    logger.info(
                        "Email draft scanner: created draft #%d for user=%d subject=%r",
                        action_id, user_id, original_subject,
                    )
                else:
                    logger.warning(
                        "Email draft scanner: failed to create draft for user=%d source_ref=%s",
                        user_id, source_ref,
                    )

        except Exception as e:
            logger.error(
                "Email draft scanner: error processing user=%d: %s", user_id, repr(e)
            )

    logger.info(
        "Email draft scanner: processed %d users, created %d drafts",
        total_users, total_created,
    )
    from web.core.database import update_heartbeat as _update_heartbeat
    _update_heartbeat("email-draft-scanner")
