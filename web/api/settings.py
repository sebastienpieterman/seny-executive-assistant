"""
Settings endpoints for Seny web application.

Provides user settings management and Claude model selection.
Also provides digest settings and preview endpoints (Phase 8-07).
"""

from typing import Optional, List
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel
import httpx
import os
import re

from web.core.database import (
    get_db,
    get_digest_preferences,
    update_digest_preferences as db_update_digest_preferences,
    get_channel_exclusion_preferences,
    update_channel_exclusion_preferences as db_update_channel_exclusion,
    get_scanner_preferences,
    update_scanner_preferences as db_update_scanner_preferences,
    get_user_profile,
)
from web.auth.jwt_utils import require_auth


# Create settings router
router = APIRouter()


# Families to show in the model selector, in display order
ALLOWED_FAMILIES = ["opus", "sonnet", "haiku"]

# Max versions to show per family (e.g. 2 = show the two newest Sonnet versions)
MAX_VERSIONS_PER_FAMILY = 2

# Human-readable description per family (version-agnostic)
FAMILY_DESCRIPTIONS = {
    "opus": "Most Capable",
    "sonnet": "Balanced",
    "haiku": "Fast & Affordable",
}


# Request/Response models
class UserSettings(BaseModel):
    """User settings model."""
    claude_model: str = "claude-sonnet-4-5-20250929"


class UserProfile(BaseModel):
    """Request model for updating user profile (system prompt personalization)."""
    user_name: Optional[str] = None
    user_pronouns_subject: Optional[str] = None
    user_pronouns_object: Optional[str] = None
    user_pronouns_possessive: Optional[str] = None
    user_context: Optional[str] = None
    key_people: Optional[str] = None  # JSON string
    key_projects: Optional[str] = None  # JSON string
    priorities: Optional[str] = None
    personality_casual: Optional[str] = None


class UserProfileResponse(BaseModel):
    """Response model for user profile."""
    user_name: Optional[str] = None
    user_pronouns_subject: str = "they"
    user_pronouns_object: str = "them"
    user_pronouns_possessive: str = "their"
    user_context: Optional[str] = None
    key_people: Optional[str] = None
    key_projects: Optional[str] = None
    priorities: Optional[str] = None
    setup_complete: bool = False
    personality_casual: bool = False


class ModelInfo(BaseModel):
    """Claude model information."""
    id: str
    display_name: str
    description: Optional[str] = None


class ModelsResponse(BaseModel):
    """Response containing available Claude models."""
    models: List[ModelInfo]
    current_model: str


class SettingsResponse(BaseModel):
    """Response for settings endpoint."""
    claude_model: str


class DigestSettings(BaseModel):
    """Digest preferences model."""
    digest_enabled: bool = True
    digest_time: str = "07:00"
    digest_email: bool = True
    digest_push: bool = True
    digest_timezone: str = "America/Chicago"


class DigestSettingsResponse(BaseModel):
    """Response for digest settings endpoint."""
    digest_enabled: bool
    digest_time: str
    digest_email: bool
    digest_push: bool
    digest_timezone: str


# Helper functions
def get_user_settings(user_id: int) -> dict:
    """
    Get user settings from database.

    Creates default settings if they don't exist.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT claude_model FROM user_settings WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()

        if row:
            return {
                "claude_model": row["claude_model"],
            }

        # Create default settings for new user
        cursor.execute(
            """
            INSERT INTO user_settings (user_id, claude_model)
            VALUES (%s, %s)
            """,
            (user_id, "claude-sonnet-4-5-20250929")
        )
        conn.commit()
        return {
            "claude_model": "claude-sonnet-4-5-20250929",
        }


def update_user_settings(user_id: int, claude_model: str) -> bool:
    """Update user settings in database."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Upsert: update if exists, insert if not
        cursor.execute(
            """
            INSERT INTO user_settings (user_id, claude_model, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                claude_model = excluded.claude_model,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, claude_model)
        )
        conn.commit()
        return True


def parse_model_id(model_id: str) -> Optional[dict]:
    """
    Parse a Claude model ID into components. Handles two formats:
      With minor:    claude-{family}-{major}-{minor}-{date}  e.g. claude-sonnet-4-6-20260115
      Without minor: claude-{family}-{major}-{date}          e.g. claude-opus-4-20250514

    The key distinction: minor is always 1-2 digits; date is always 8 digits.
    Returns None if the ID doesn't match the expected format or family.
    """
    match = re.match(r'claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-(\d{8}))?$', model_id)
    if not match:
        return None
    family, major, minor, date = match.groups()
    if family not in ALLOWED_FAMILIES:
        return None
    minor_int = int(minor) if minor is not None else 0
    return {
        "family": family,
        "major": int(major),
        "minor": minor_int,
        "date": date,
        # Tuple used for sorting: higher = newer
        "version_tuple": (int(major), minor_int, date),
    }


def get_model_display_name(model_id: str) -> str:
    """Generate a human-readable display name from a model ID."""
    parsed = parse_model_id(model_id)
    if not parsed:
        return model_id.replace("-", " ").title()
    family = parsed["family"].capitalize()
    # Only show minor version if it's non-zero (e.g. "4.6" not "4.0")
    version = f"{parsed['major']}.{parsed['minor']}" if parsed["minor"] else str(parsed["major"])
    desc = FAMILY_DESCRIPTIONS.get(parsed["family"], "")
    return f"Claude {family} {version} ({desc})" if desc else f"Claude {family} {version}"


def is_allowed_model(model_id: str) -> bool:
    """Check if model should be shown in the selector."""
    return parse_model_id(model_id) is not None


@router.get("", response_model=SettingsResponse)
async def get_settings(user_id: str = Depends(require_auth)):
    """
    Get current user settings.

    Returns the user's saved preferences including their selected Claude model
    and local LLM configuration.
    """
    settings = get_user_settings(int(user_id))
    return SettingsResponse(
        claude_model=settings["claude_model"],
    )


@router.put("", response_model=SettingsResponse)
async def update_settings(
    settings: UserSettings,
    user_id: str = Depends(require_auth)
):
    """
    Update user settings.

    Allows users to change their Claude model preference.
    """
    # Validate the model exists (basic check against allowed prefixes)
    if not is_allowed_model(settings.claude_model):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid model: {settings.claude_model}"
        )

    success = update_user_settings(int(user_id), settings.claude_model)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update settings"
        )

    return SettingsResponse(
        claude_model=settings.claude_model,
    )


@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(user_id: str = Depends(require_auth)):
    """Get user profile for system prompt personalization."""
    profile = get_user_profile(int(user_id))
    return UserProfileResponse(**profile)


@router.patch("/profile", response_model=UserProfileResponse)
async def update_profile(
    profile: UserProfile,
    user_id: str = Depends(require_auth)
):
    """
    Update user profile. Only provided fields are updated.

    Used to configure system prompt personalization (name, pronouns,
    context, key people/projects, priorities).
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Ensure user_settings row exists
        cursor.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (%s)",
            (int(user_id),)
        )

        updates = []
        values = []

        if profile.user_name is not None:
            updates.append("user_name = %s")
            values.append(profile.user_name or None)

        if profile.user_pronouns_subject is not None:
            updates.append("user_pronouns_subject = %s")
            values.append(profile.user_pronouns_subject or 'they')

        if profile.user_pronouns_object is not None:
            updates.append("user_pronouns_object = %s")
            values.append(profile.user_pronouns_object or 'them')

        if profile.user_pronouns_possessive is not None:
            updates.append("user_pronouns_possessive = %s")
            values.append(profile.user_pronouns_possessive or 'their')

        if profile.user_context is not None:
            updates.append("user_context = %s")
            values.append(profile.user_context or None)

        if profile.key_people is not None:
            updates.append("key_people = %s")
            values.append(profile.key_people or None)

        if profile.key_projects is not None:
            updates.append("key_projects = %s")
            values.append(profile.key_projects or None)

        if profile.priorities is not None:
            updates.append("priorities = %s")
            values.append(profile.priorities or None)

        if profile.personality_casual is not None:
            updates.append("personality_casual = %s")
            values.append(1 if profile.personality_casual in ("1", "true", True) else 0)

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            values.append(int(user_id))
            cursor.execute(
                f"UPDATE user_settings SET {', '.join(updates)} WHERE user_id = %s",
                values
            )
            conn.commit()

    current = get_user_profile(int(user_id))
    return UserProfileResponse(**current)


@router.patch("/default-calendar")
async def update_default_calendar(
    data: dict,
    user_id: str = Depends(require_auth)
):
    """Set the preferred Google account for calendar proposal approval."""
    email = data.get("email") or None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET default_calendar_email = %s WHERE user_id = %s",
            (email, int(user_id))
        )
        conn.commit()
    return {"default_calendar_email": email}


@router.post("/screen-agent/key")
async def generate_screen_agent_key(user_id: str = Depends(require_auth)):
    """Generate (or regenerate) the static API key for the screen agent."""
    import secrets
    key = secrets.token_urlsafe(32)
    from web.core.database import set_screen_agent_key, get_screen_agent_key
    set_screen_agent_key(int(user_id), key)
    return {"key": key}


@router.get("/screen-agent/key")
async def get_screen_agent_key_endpoint(user_id: str = Depends(require_auth)):
    """Retrieve the current screen agent API key (returns null if not yet generated)."""
    from web.core.database import get_screen_agent_key
    key = get_screen_agent_key(int(user_id))
    return {"key": key}


@router.get("/models", response_model=ModelsResponse)
async def list_models(user_id: str = Depends(require_auth)):
    """
    List available Claude models.

    Fetches the list of available models from the Anthropic API
    and filters to show only the main model families.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Anthropic API key not configured"
        )

    # Get user's current model preference
    settings = get_user_settings(int(user_id))
    current_model = settings["claude_model"]

    try:
        # Fetch models from Anthropic API
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"
                },
                timeout=10.0
            )

            if response.status_code != 200:
                # Fall back to hardcoded list if API fails
                return _fallback_models_response(current_model, settings)

            data = response.json()

            # Group parsed models by family, collect all valid versions
            from collections import defaultdict
            families: dict = defaultdict(list)

            for model in data.get("data", []):
                model_id = model.get("id", "")
                parsed = parse_model_id(model_id)
                if not parsed:
                    continue
                families[parsed["family"]].append((parsed["version_tuple"], model_id))

            # For each family, take the top MAX_VERSIONS_PER_FAMILY newest versions
            models = []
            for family in ALLOWED_FAMILIES:
                top_versions = sorted(
                    families[family],
                    key=lambda x: x[0],
                    reverse=True
                )[:MAX_VERSIONS_PER_FAMILY]
                for _, model_id in top_versions:
                    models.append(ModelInfo(
                        id=model_id,
                        display_name=get_model_display_name(model_id),
                        description=None,
                    ))

            # If no models found, use fallback
            if not models:
                return _fallback_models_response(current_model)

            return ModelsResponse(models=models, current_model=current_model)

    except Exception as e:
        print(f"Error fetching models from Anthropic API: {e}")
        # Fall back to hardcoded list
        return _fallback_models_response(current_model)


def _fallback_models_response(current_model: str) -> ModelsResponse:
    """Return hardcoded model list when Anthropic API is unavailable."""
    fallback_ids = [
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-6-20260115",
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
    ]
    models = [
        ModelInfo(id=mid, display_name=get_model_display_name(mid), description=None)
        for mid in fallback_ids
    ]

    return ModelsResponse(models=models, current_model=current_model)


# ============================================================================
# Digest Settings Endpoints
# ============================================================================

@router.get("/digest", response_model=DigestSettingsResponse)
async def get_digest_settings(user_id: str = Depends(require_auth)):
    """
    Get current digest preferences.

    Returns the user's digest delivery settings (time, email, push, etc).
    """
    prefs = get_digest_preferences(int(user_id))
    return DigestSettingsResponse(**prefs)


@router.put("/digest", response_model=DigestSettingsResponse)
async def update_digest_settings(
    settings: DigestSettings,
    user_id: str = Depends(require_auth)
):
    """
    Update digest preferences.

    Allows users to change their daily digest delivery settings.
    """
    # Validate timezone (basic check)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(settings.digest_timezone)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timezone: {settings.digest_timezone}"
        )

    # Validate time format (HH:MM)
    import re
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', settings.digest_time):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid time format: {settings.digest_time}. Use HH:MM format."
        )

    success = db_update_digest_preferences(
        user_id=int(user_id),
        digest_enabled=settings.digest_enabled,
        digest_time=settings.digest_time,
        digest_email=settings.digest_email,
        digest_push=settings.digest_push,
        digest_timezone=settings.digest_timezone
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update digest settings"
        )

    return DigestSettingsResponse(
        digest_enabled=settings.digest_enabled,
        digest_time=settings.digest_time,
        digest_email=settings.digest_email,
        digest_push=settings.digest_push,
        digest_timezone=settings.digest_timezone
    )


@router.get("/digest/today")
async def get_todays_digest(user_id: str = Depends(require_auth)):
    """
    Get today's digest (preview without sending).

    Returns the generated digest data for display in the app.
    """
    from web.services.digest_service import DigestService

    digest_service = DigestService(int(user_id))
    digest = await digest_service.generate_daily_digest()

    return digest


@router.post("/digest/send-now")
async def send_digest_now(user_id: str = Depends(require_auth)):
    """
    Send digest immediately (for testing).

    Generates and delivers the digest according to user preferences.
    """
    from web.services.digest_service import DigestService

    digest_service = DigestService(int(user_id))
    result = await digest_service.deliver_digest()

    return {
        "success": result.get("generated", False),
        "email_sent": result.get("email_sent", False),
        "push_sent": result.get("push_sent", False),
        "reason": result.get("reason")
    }


# ============================================================================
# Weekly Review Settings Endpoints
# ============================================================================

class WeeklyReviewSettings(BaseModel):
    """Weekly review preferences model."""
    weekly_review_enabled: bool = True
    weekly_review_day: str = "sunday"
    weekly_review_time: str = "18:00"


class WeeklyReviewSettingsResponse(BaseModel):
    """Response for weekly review settings endpoint."""
    weekly_review_enabled: bool
    weekly_review_day: str
    weekly_review_time: str
    timezone: str


@router.get("/weekly-review", response_model=WeeklyReviewSettingsResponse)
async def get_weekly_review_settings(user_id: str = Depends(require_auth)):
    """
    Get current weekly review preferences.

    Returns the user's weekly review delivery settings (day, time).
    """
    from web.core.database import get_weekly_review_preferences
    prefs = get_weekly_review_preferences(int(user_id))
    return WeeklyReviewSettingsResponse(**prefs)


@router.put("/weekly-review", response_model=WeeklyReviewSettingsResponse)
async def update_weekly_review_settings(
    settings: WeeklyReviewSettings,
    user_id: str = Depends(require_auth)
):
    """
    Update weekly review preferences.

    Allows users to change their weekly review delivery settings.
    """
    from web.core.database import update_weekly_review_preferences, get_weekly_review_preferences

    # Validate day
    valid_days = ['sunday', 'saturday', 'friday', 'monday', 'tuesday', 'wednesday', 'thursday']
    if settings.weekly_review_day.lower() not in valid_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid day: {settings.weekly_review_day}. Must be one of: {', '.join(valid_days)}"
        )

    # Validate time format (HH:MM)
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', settings.weekly_review_time):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid time format: {settings.weekly_review_time}. Use HH:MM format."
        )

    success = update_weekly_review_preferences(
        user_id=int(user_id),
        weekly_review_enabled=settings.weekly_review_enabled,
        weekly_review_day=settings.weekly_review_day.lower(),
        weekly_review_time=settings.weekly_review_time
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update weekly review settings"
        )

    prefs = get_weekly_review_preferences(int(user_id))
    return WeeklyReviewSettingsResponse(**prefs)


@router.get("/weekly-review/current")
async def get_current_weekly_review(user_id: str = Depends(require_auth), mode: str = "template"):
    """
    Get the current week's review (preview without sending).

    Returns the generated weekly review data for display in the app.
    Pass ?mode=claude to use the Sonnet-powered deep reasoning review.
    """
    from web.services.digest_service import DigestService
    import traceback

    try:
        print(f"[weekly-review/current] Starting for user {user_id} mode={mode}", flush=True)
        digest_service = DigestService(int(user_id))
        if mode == "claude":
            review = await digest_service.generate_claude_weekly_review()
        else:
            review = await digest_service.generate_weekly_review()
        print(f"[weekly-review/current] Generated successfully", flush=True)
        return review
    except Exception as e:
        print(f"[weekly-review/current] ERROR: {e}", flush=True)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/weekly-review/generate")
async def generate_weekly_review_now(user_id: str = Depends(require_auth), mode: str = "template"):
    """
    Force generate a new weekly review (for manual trigger/testing).

    Returns the freshly generated review.
    Pass ?mode=claude to use the Sonnet-powered deep reasoning review.
    """
    from web.services.digest_service import DigestService
    import traceback

    try:
        print(f"[weekly-review/generate] Starting for user {user_id} mode={mode}", flush=True)
        digest_service = DigestService(int(user_id))
        if mode == "claude":
            review = await digest_service.generate_claude_weekly_review()
        else:
            review = await digest_service.generate_weekly_review()
        print(f"[weekly-review/generate] Generated successfully", flush=True)
        return review
    except Exception as e:
        print(f"[weekly-review/generate] ERROR: {e}", flush=True)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/weekly-review/send-now")
async def send_weekly_review_now(user_id: str = Depends(require_auth)):
    """
    Send weekly review immediately (for testing).

    Generates and delivers the weekly review according to user preferences.
    """
    from web.services.digest_service import DigestService
    import traceback

    try:
        print(f"[weekly-review/send-now] Starting for user {user_id}", flush=True)
        digest_service = DigestService(int(user_id))
        result = await digest_service.deliver_weekly_review()
        print(f"[weekly-review/send-now] Delivered successfully", flush=True)
        return {
            "success": result.get("generated", False),
            "email_sent": result.get("email_sent", False),
            "push_sent": result.get("push_sent", False),
            "reason": result.get("reason")
        }
    except Exception as e:
        print(f"[weekly-review/send-now] ERROR: {e}", flush=True)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Channel Exclusion Settings Endpoints
# ============================================================================

class ChannelExclusionPreferences(BaseModel):
    """Channel exclusion preferences model."""
    slack_excluded_channels: List[str] = []
    telegram_excluded_chats: List[str] = []


class ChannelInfo(BaseModel):
    """Information about a channel/chat."""
    id: str
    name: str
    excluded: bool
    type: Optional[str] = None  # For Telegram: 'group', 'channel', 'private'
    workspace_id: Optional[str] = None  # For Slack: team_id
    workspace_name: Optional[str] = None  # For Slack: workspace name


class SlackChannelsResponse(BaseModel):
    """Response for Slack channels list."""
    connected: bool
    channels: List[ChannelInfo] = []


class TelegramChatsResponse(BaseModel):
    """Response for Telegram chats list."""
    connected: bool
    chats: List[ChannelInfo] = []


@router.get("/channel-exclusion", response_model=ChannelExclusionPreferences)
async def get_channel_exclusion_settings(user_id: str = Depends(require_auth)):
    """
    Get current channel exclusion preferences.

    Returns lists of excluded Slack channel IDs and Telegram chat IDs.
    """
    prefs = get_channel_exclusion_preferences(int(user_id))
    return ChannelExclusionPreferences(**prefs)


@router.put("/channel-exclusion", response_model=ChannelExclusionPreferences)
async def update_channel_exclusion_settings(
    settings: ChannelExclusionPreferences,
    user_id: str = Depends(require_auth)
):
    """
    Update channel exclusion preferences.

    Allows users to specify which Slack channels and Telegram chats to exclude
    from the scanner.
    """
    success = db_update_channel_exclusion(
        user_id=int(user_id),
        slack_excluded_channels=settings.slack_excluded_channels,
        telegram_excluded_chats=settings.telegram_excluded_chats
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update channel exclusion settings"
        )

    return ChannelExclusionPreferences(
        slack_excluded_channels=settings.slack_excluded_channels,
        telegram_excluded_chats=settings.telegram_excluded_chats
    )


@router.get("/channel-exclusion/slack-channels", response_model=SlackChannelsResponse)
async def get_slack_channels_for_exclusion(user_id: str = Depends(require_auth)):
    """
    Get list of all Slack channels for the exclusion UI.

    Returns all connected Slack channels with their current exclusion status.
    """
    from web.services.slack_service import SlackService

    # Get current exclusion preferences
    prefs = get_channel_exclusion_preferences(int(user_id))
    excluded_ids = set(prefs.get('slack_excluded_channels', []))

    # Check if user has any Slack workspace connected
    workspaces = SlackService.list_connected_workspaces(int(user_id))
    if not workspaces:
        return SlackChannelsResponse(connected=False, channels=[])

    all_channels = []

    for workspace in workspaces:
        team_id = workspace.get('team_id')
        if not team_id:
            continue

        try:
            slack = SlackService(int(user_id), team_id)
            if not slack.is_connected():
                continue

            # Get workspace name
            workspace_name = workspace.get('team_name', team_id)

            # Get channels
            channels = await slack.list_channels(
                types="public_channel,private_channel", limit=100
            )

            # Also get DMs
            dms = await slack.list_dms(limit=50)

            # Add channels
            for ch in channels:
                channel_id = ch['id']
                all_channels.append(ChannelInfo(
                    id=channel_id,
                    name=f"#{ch.get('name', channel_id)}",
                    excluded=channel_id in excluded_ids,
                    type="channel",
                    workspace_id=team_id,
                    workspace_name=workspace_name
                ))

            # Add DMs
            for dm in dms:
                dm_id = dm['id']
                dm_name = dm.get('user_name') or dm.get('user_id', dm_id)
                all_channels.append(ChannelInfo(
                    id=dm_id,
                    name=f"@{dm_name}",
                    excluded=dm_id in excluded_ids,
                    type="dm",
                    workspace_id=team_id,
                    workspace_name=workspace_name
                ))

        except Exception as e:
            print(f"[channel-exclusion] Error getting Slack channels: {e}")
            continue

    return SlackChannelsResponse(connected=True, channels=all_channels)


@router.get("/channel-exclusion/telegram-chats", response_model=TelegramChatsResponse)
async def get_telegram_chats_for_exclusion(user_id: str = Depends(require_auth)):
    """
    Get list of all Telegram chats for the exclusion UI.

    Returns all connected Telegram chats with their current exclusion status.
    """
    from web.services.telegram_service import TelegramService

    # Get current exclusion preferences
    prefs = get_channel_exclusion_preferences(int(user_id))
    excluded_ids = set(prefs.get('telegram_excluded_chats', []))

    telegram = TelegramService(int(user_id))
    if not telegram.is_configured() or not telegram.is_connected():
        return TelegramChatsResponse(connected=False, chats=[])

    if not await telegram.connect():
        return TelegramChatsResponse(connected=False, chats=[])

    all_chats = []

    try:
        dialogs = await telegram.list_dialogs(limit=50)

        for dialog in dialogs:
            chat_id = str(dialog.get('id'))
            chat_name = dialog.get('name', chat_id)
            chat_type = dialog.get('type', 'private')

            all_chats.append(ChannelInfo(
                id=chat_id,
                name=chat_name,
                excluded=chat_id in excluded_ids,
                type=chat_type
            ))

    except Exception as e:
        print(f"[channel-exclusion] Error getting Telegram chats: {e}")

    return TelegramChatsResponse(connected=True, chats=all_chats)


# ============================================================================
# Learned Patterns Settings Endpoints
# ============================================================================

class LearnedPatternsResponse(BaseModel):
    """Response for learned patterns endpoint."""
    responsive_hours: List[int] = []
    item_type_preferences: dict = {}
    last_computed_at: Optional[str] = None
    has_data: bool = False


@router.get("/patterns", response_model=LearnedPatternsResponse)
async def get_learned_patterns(user_id: str = Depends(require_auth)):
    """
    Get learned pattern preferences for a user.

    Returns responsive hours, item type preferences, and when they were last computed.
    """
    from web.core.database import get_pattern_preferences
    import json

    patterns = get_pattern_preferences(int(user_id))

    if not patterns:
        return LearnedPatternsResponse(
            responsive_hours=[],
            item_type_preferences={},
            last_computed_at=None,
            has_data=False
        )

    # Parse JSON fields
    responsive_hours = []
    if patterns.get('responsive_hours'):
        try:
            responsive_hours = json.loads(patterns['responsive_hours'])
        except (json.JSONDecodeError, TypeError):
            responsive_hours = []

    item_type_preferences = {}
    if patterns.get('item_type_preferences'):
        try:
            item_type_preferences = json.loads(patterns['item_type_preferences'])
        except (json.JSONDecodeError, TypeError):
            item_type_preferences = {}

    return LearnedPatternsResponse(
        responsive_hours=responsive_hours,
        item_type_preferences=item_type_preferences,
        last_computed_at=patterns.get('last_computed_at'),
        has_data=bool(responsive_hours or item_type_preferences)
    )


@router.post("/patterns/reset")
async def reset_learned_patterns(user_id: str = Depends(require_auth)):
    """
    Reset learned pattern preferences for a user.

    Clears all learned patterns, allowing the system to start fresh.
    """
    from web.core.database import get_db

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM user_pattern_preferences
                WHERE user_id = %s
            """, (int(user_id),))
            deleted = cursor.rowcount > 0

        return {
            "success": True,
            "message": "Learned preferences reset successfully" if deleted else "No preferences to reset"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset patterns: {str(e)}"
        )


# ============================================================================
# Scanner Settings Endpoints
# ============================================================================

class ScannerSettings(BaseModel):
    """Scanner preferences model."""
    scanner_gmail_interval_minutes: Optional[int] = None
    scanner_slack_interval_minutes: Optional[int] = None
    scanner_telegram_interval_minutes: Optional[int] = None
    scanner_calendar_interval_minutes: Optional[int] = None
    classification_tier: Optional[str] = None


class ScannerSettingsResponse(BaseModel):
    """Response for scanner settings endpoint."""
    scanner_gmail_interval_minutes: int
    scanner_slack_interval_minutes: int
    scanner_telegram_interval_minutes: int
    scanner_calendar_interval_minutes: int
    classification_tier: str


@router.get("/scanner", response_model=ScannerSettingsResponse)
async def get_scanner_settings(user_id: str = Depends(require_auth)):
    """
    Get current scanner preferences.

    Returns the user's scanner interval settings and classification tier.
    """
    prefs = get_scanner_preferences(int(user_id))
    return ScannerSettingsResponse(**prefs)


@router.put("/scanner", response_model=ScannerSettingsResponse)
async def update_scanner_settings(
    settings: ScannerSettings,
    user_id: str = Depends(require_auth)
):
    """
    Update scanner preferences.

    Allows users to change per-source scan intervals and AI classification tier.
    """
    # Validate intervals (5-1440 minutes)
    interval_fields = [
        ('scanner_gmail_interval_minutes', settings.scanner_gmail_interval_minutes),
        ('scanner_slack_interval_minutes', settings.scanner_slack_interval_minutes),
        ('scanner_telegram_interval_minutes', settings.scanner_telegram_interval_minutes),
        ('scanner_calendar_interval_minutes', settings.scanner_calendar_interval_minutes),
    ]

    for field_name, value in interval_fields:
        if value is not None:
            if value < 5 or value > 1440:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid {field_name}: must be between 5 and 1440 minutes"
                )

    # Validate tier
    if settings.classification_tier is not None:
        if settings.classification_tier not in ('haiku', 'full'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid classification_tier: must be 'haiku' or 'full'"
            )

    success = db_update_scanner_preferences(
        user_id=int(user_id),
        scanner_gmail_interval_minutes=settings.scanner_gmail_interval_minutes,
        scanner_slack_interval_minutes=settings.scanner_slack_interval_minutes,
        scanner_telegram_interval_minutes=settings.scanner_telegram_interval_minutes,
        scanner_calendar_interval_minutes=settings.scanner_calendar_interval_minutes,
        classification_tier=settings.classification_tier
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update scanner settings"
        )

    # Return updated preferences
    prefs = get_scanner_preferences(int(user_id))
    return ScannerSettingsResponse(**prefs)


# ============================================================================
# Multi-Channel Chat Settings Endpoints
# ============================================================================

class MultiChannelChatSettingsRequest(BaseModel):
    """Request model for updating multi-channel chat settings."""
    telegram_chat_enabled: Optional[bool] = None
    slack_chat_enabled: Optional[bool] = None


class MultiChannelChatSettingsResponse(BaseModel):
    """Response for multi-channel chat settings endpoint."""
    telegram_chat_enabled: bool
    slack_chat_enabled: bool
    telegram_bot_linked: bool
    slack_bot_linked: bool
    telegram_bot_configured: bool


@router.get("/multichannel-chat", response_model=MultiChannelChatSettingsResponse)
async def get_multichannel_chat_settings_endpoint(user_id: str = Depends(require_auth)):
    """
    Get multi-channel chat settings.

    Returns enabled/disabled status for Telegram and Slack bot chat,
    plus link status indicating if the user has connected their accounts.
    """
    from web.core.database import (
        get_multichannel_chat_settings,
        get_telegram_bot_user_links_for_user,
        get_slack_bot_token,
    )
    from web.services.telegram_bot_service import TelegramBotService

    # Get settings
    settings = get_multichannel_chat_settings(int(user_id))

    # Check if Telegram bot is configured (env var set)
    telegram_bot = TelegramBotService()
    telegram_bot_configured = telegram_bot.is_configured()

    # Check if user has linked their Telegram account
    telegram_links = get_telegram_bot_user_links_for_user(int(user_id))
    telegram_bot_linked = len(telegram_links) > 0

    # Check if user has Slack bot token
    slack_bot_info = get_slack_bot_token(int(user_id))
    slack_bot_linked = slack_bot_info is not None and slack_bot_info.get("bot_token") is not None

    return MultiChannelChatSettingsResponse(
        telegram_chat_enabled=settings.get("telegram_chat_enabled", True),
        slack_chat_enabled=settings.get("slack_chat_enabled", True),
        telegram_bot_linked=telegram_bot_linked,
        slack_bot_linked=slack_bot_linked,
        telegram_bot_configured=telegram_bot_configured
    )


@router.patch("/multichannel-chat", response_model=MultiChannelChatSettingsResponse)
async def update_multichannel_chat_settings_endpoint(
    request: MultiChannelChatSettingsRequest,
    user_id: str = Depends(require_auth)
):
    """
    Update multi-channel chat settings.

    Allows users to enable/disable Telegram and Slack bot chat.
    """
    from web.core.database import (
        update_multichannel_chat_settings,
        get_multichannel_chat_settings,
        get_telegram_bot_user_links_for_user,
        get_slack_bot_token,
    )
    from web.services.telegram_bot_service import TelegramBotService

    success = update_multichannel_chat_settings(
        user_id=int(user_id),
        telegram_chat_enabled=request.telegram_chat_enabled,
        slack_chat_enabled=request.slack_chat_enabled
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update multi-channel chat settings"
        )

    # Get updated settings and link status
    settings = get_multichannel_chat_settings(int(user_id))

    telegram_bot = TelegramBotService()
    telegram_bot_configured = telegram_bot.is_configured()

    telegram_links = get_telegram_bot_user_links_for_user(int(user_id))
    telegram_bot_linked = len(telegram_links) > 0

    slack_bot_info = get_slack_bot_token(int(user_id))
    slack_bot_linked = slack_bot_info is not None and slack_bot_info.get("bot_token") is not None

    return MultiChannelChatSettingsResponse(
        telegram_chat_enabled=settings.get("telegram_chat_enabled", True),
        slack_chat_enabled=settings.get("slack_chat_enabled", True),
        telegram_bot_linked=telegram_bot_linked,
        slack_bot_linked=slack_bot_linked,
        telegram_bot_configured=telegram_bot_configured
    )


class TelegramLinkRequest(BaseModel):
    """Request model for linking Telegram chat."""
    chat_id: str


class TelegramLinkResponse(BaseModel):
    """Response for Telegram link endpoints."""
    success: bool
    message: str
    linked_chats: List[dict] = []


@router.post("/multichannel-chat/telegram-link", response_model=TelegramLinkResponse)
async def link_telegram_chat(
    request: TelegramLinkRequest,
    user_id: str = Depends(require_auth)
):
    """
    Link a Telegram chat to the current user.

    The user gets the chat_id code from the Telegram bot's welcome message.
    """
    from web.core.database import (
        create_telegram_bot_user_link,
        get_telegram_bot_user_link,
        get_telegram_bot_user_links_for_user,
    )

    try:
        chat_id = int(request.chat_id.strip())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid chat ID. Please enter the numeric code from the bot."
        )

    # Check if this chat is already linked to someone
    existing = get_telegram_bot_user_link(chat_id)
    if existing:
        if existing["user_id"] == int(user_id):
            return TelegramLinkResponse(
                success=True,
                message="This Telegram chat is already linked to your account.",
                linked_chats=[{"chat_id": str(chat_id)}]
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This Telegram chat is already linked to another account."
            )

    # Create the link
    create_telegram_bot_user_link(
        user_id=int(user_id),
        telegram_chat_id=chat_id,
        telegram_username=None,
        telegram_first_name=None
    )

    # Get updated list
    links = get_telegram_bot_user_links_for_user(int(user_id))

    return TelegramLinkResponse(
        success=True,
        message="Telegram chat linked successfully! You can now chat with Seny.",
        linked_chats=[{"chat_id": str(link["telegram_chat_id"])} for link in links]
    )


@router.delete("/multichannel-chat/telegram-link/{chat_id}")
async def unlink_telegram_chat(
    chat_id: str,
    user_id: str = Depends(require_auth)
):
    """
    Unlink a Telegram chat from the current user.
    """
    from web.core.database import (
        get_telegram_bot_user_link,
        delete_telegram_bot_user_link,
    )

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid chat ID."
        )

    # Verify this chat belongs to the user
    existing = get_telegram_bot_user_link(chat_id_int)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Telegram chat not found."
        )

    if existing["user_id"] != int(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only unlink your own Telegram chats."
        )

    delete_telegram_bot_user_link(chat_id_int)

    return {"success": True, "message": "Telegram chat unlinked."}


@router.post("/setup/complete")
async def complete_setup(user_id: str = Depends(require_auth)):
    """Mark setup wizard as complete.

    Validates that user_name is set. If not, returns a warning
    but still marks setup_complete=true (user chose to skip).
    """
    profile = get_user_profile(int(user_id))

    warnings = []
    if not profile.get('user_name') or profile['user_name'] == 'User':
        warnings.append("No name set — Seny will address you as 'User'")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET setup_complete = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
            (int(user_id),)
        )

    return {
        "success": True,
        "setup_complete": True,
        "warnings": warnings
    }


@router.post("/setup/reset")
async def reset_setup(user_id: str = Depends(require_auth)):
    """Reset setup wizard — allows user to re-run the wizard.

    Does NOT clear profile data. User's existing name, pronouns,
    context, etc. are preserved. Only resets the setup_complete flag.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET setup_complete = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
            (int(user_id),)
        )

    return {
        "success": True,
        "setup_complete": False,
        "message": "Setup wizard reset. You'll be redirected to the wizard on next page load."
    }
