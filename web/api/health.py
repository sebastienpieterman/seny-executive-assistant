"""
Health Check API - Phase 21-03

Provides health check endpoint for Railway monitoring and webhook status.
Supports ?detailed=true for webhook infrastructure health reporting.
"""

import os
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check(detailed: bool = False):
    """
    Health check endpoint for Railway monitoring.

    Args:
        detailed: If True, include webhook health status.

    Returns:
        dict: Status information, with optional webhook details.
    """
    print("✓ Health check pinged")

    result = {
        "status": "healthy",
        "environment": "production" if os.getenv("RAILWAY_ENVIRONMENT") else "development"
    }

    if detailed:
        from web.services.webhook_health_service import check_telegram_webhook, check_slack_events

        try:
            telegram_health = await check_telegram_webhook()
        except Exception as e:
            logger.error(f"Telegram health check failed: {repr(e)}")
            telegram_health = {"healthy": False, "error": repr(e)}

        try:
            slack_health = check_slack_events()
        except Exception as e:
            logger.error(f"Slack health check failed: {repr(e)}")
            slack_health = {"configured": False, "error": repr(e)}

        result["webhooks"] = {
            "telegram": telegram_health,
            "slack": slack_health,
        }

    return result


@router.get("/health/slack-drip")
async def slack_drip_status():
    """
    Diagnostic endpoint for Slack drip scanner state.
    No authentication required — for operational monitoring.

    Returns channel cursor counts, circuit breaker states, and recent errors
    per workspace so we can confirm channels are registered and scanning.
    """
    from web.core.database import get_db

    try:
        with get_db() as db:
            cursor = db.cursor()

            # Summary per workspace
            rows = cursor.execute("""
                SELECT
                    team_id,
                    COUNT(*) as total_channels,
                    SUM(CASE WHEN is_excluded = 1 THEN 1 ELSE 0 END) as excluded,
                    SUM(CASE WHEN circuit_state = 'open' THEN 1 ELSE 0 END) as circuit_open,
                    SUM(CASE WHEN circuit_state = 'half_open' THEN 1 ELSE 0 END) as circuit_half_open,
                    SUM(CASE WHEN last_scan_ts IS NOT NULL THEN 1 ELSE 0 END) as scanned,
                    MAX(last_scan_at) as most_recent_scan,
                    COUNT(DISTINCT user_id) as users
                FROM slack_channel_cursors
                GROUP BY team_id
            """).fetchall()

            # Recent errors
            errors = cursor.execute("""
                SELECT team_id, channel_name, last_error, consecutive_failures, circuit_state
                FROM slack_channel_cursors
                WHERE last_error IS NOT NULL AND last_error != ''
                ORDER BY updated_at DESC
                LIMIT 10
            """).fetchall()

            workspaces = []
            for row in rows:
                workspaces.append({
                    "team_id": row["team_id"],
                    "total_channels": row["total_channels"],
                    "excluded": row["excluded"],
                    "circuit_open": row["circuit_open"],
                    "circuit_half_open": row["circuit_half_open"],
                    "scanned": row["scanned"],
                    "most_recent_scan": row["most_recent_scan"],
                    "users": row["users"],
                })

            recent_errors = []
            for row in errors:
                recent_errors.append({
                    "team_id": row["team_id"],
                    "channel_name": row["channel_name"],
                    "last_error": row["last_error"],
                    "consecutive_failures": row["consecutive_failures"],
                    "circuit_state": row["circuit_state"],
                })

            # Token status — tells us if workspaces are actually connected
            token_rows = cursor.execute("""
                SELECT user_id, team_id, team_name, authed_user_name, created_at
                FROM slack_tokens
                ORDER BY user_id, created_at
            """).fetchall()

            tokens = [
                {
                    "user_id": r[0],
                    "team_id": r[1],
                    "team_name": r[2],
                    "authed_user_name": r[3],
                    "connected_at": r[4],
                }
                for r in token_rows
            ]

            return {
                "tokens_in_db": tokens,
                "workspaces": workspaces,
                "total_registered": sum(w["total_channels"] for w in workspaces),
                "recent_errors": recent_errors,
            }

    except Exception as e:
        return {"error": repr(e)}
