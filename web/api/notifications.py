"""
Notifications API for Seny - Phase 7 (07-09).

Push notifications and timer/alarm management:
- POST /api/notifications/subscribe - Register push subscription
- DELETE /api/notifications/subscribe - Unsubscribe from push
- GET /api/notifications/devices - List subscribed devices
- POST /api/notifications/test - Send test notification
- GET /api/notifications/vapid-public-key - Get VAPID public key

Timer endpoints:
- POST /api/timers - Create timer
- GET /api/timers - List active timers
- DELETE /api/timers/{id} - Cancel timer

Alarm endpoints:
- POST /api/alarms - Create alarm
- GET /api/alarms - List active alarms
- DELETE /api/alarms/{id} - Cancel alarm
"""

import logging
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, Request
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


# Create notifications router
router = APIRouter()


# ============================================================================
# Request/Response Models
# ============================================================================

class PushSubscription(BaseModel):
    """Push subscription registration request."""
    endpoint: str
    keys: dict  # Contains p256dh and auth


class SubscriptionResponse(BaseModel):
    """Response for subscription operations."""
    id: int
    device_name: str


class DeviceInfo(BaseModel):
    """Information about a subscribed device."""
    id: int
    device_name: str
    created_at: str
    last_used_at: Optional[str]


class DevicesResponse(BaseModel):
    """Response for listing devices."""
    devices: list[DeviceInfo]
    total: int


class TestNotificationResponse(BaseModel):
    """Response for test notification."""
    sent: int
    failed: int
    message: str


class TimerRequest(BaseModel):
    """Create timer request."""
    duration: int  # seconds
    label: Optional[str] = "Timer"


class TimerInfo(BaseModel):
    """Timer information."""
    id: int
    label: str
    fires_at: str
    remaining_seconds: int
    remaining_formatted: str


class TimerResponse(BaseModel):
    """Response for timer creation."""
    id: int
    fires_at: str
    label: str
    duration_seconds: int


class TimersListResponse(BaseModel):
    """Response for listing timers."""
    timers: list[TimerInfo]
    total: int


class AlarmRequest(BaseModel):
    """Create alarm request."""
    time: str  # ISO 8601 datetime
    label: Optional[str] = "Alarm"
    repeat: Optional[str] = None  # daily, weekdays, weekly


class AlarmInfo(BaseModel):
    """Alarm information."""
    id: int
    label: str
    fires_at: str
    repeat_pattern: Optional[str]
    timezone: str


class AlarmResponse(BaseModel):
    """Response for alarm creation."""
    id: int
    fires_at: str
    label: str
    repeat_pattern: Optional[str]


class AlarmsListResponse(BaseModel):
    """Response for listing alarms."""
    alarms: list[AlarmInfo]
    total: int


class CancelResponse(BaseModel):
    """Response for cancel operations."""
    success: bool
    message: str


# ============================================================================
# Push Subscription Endpoints
# ============================================================================

@router.get("/vapid-public-key")
async def get_vapid_public_key():
    """
    Get the VAPID public key for push subscription.

    Returns:
        Dict with public_key
    """
    public_key = os.getenv("VAPID_PUBLIC_KEY")
    if not public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notifications not configured"
        )
    return {"public_key": public_key}


@router.post("/subscribe", response_model=SubscriptionResponse)
async def subscribe_push(
    request: Request,
    subscription: PushSubscription,
    user_id: str = Depends(require_auth)
):
    """
    Register a push subscription for the current user.

    The client sends the push subscription object from the browser's
    PushManager.subscribe() call.

    Args:
        subscription: Push subscription with endpoint and keys

    Returns:
        SubscriptionResponse with id and device_name
    """
    try:
        user_agent = request.headers.get("user-agent")
        service = NotificationService(int(user_id))
        result = await service.save_subscription(
            endpoint=subscription.endpoint,
            p256dh_key=subscription.keys.get("p256dh", ""),
            auth_key=subscription.keys.get("auth", ""),
            user_agent=user_agent
        )
        return SubscriptionResponse(**result)
    except Exception as e:
        logger.error(f"Failed to save push subscription: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.delete("/subscribe")
async def unsubscribe_push(
    endpoint: str = Query(..., description="Push subscription endpoint to remove"),
    user_id: str = Depends(require_auth)
):
    """
    Remove a push subscription.

    Args:
        endpoint: The push subscription endpoint URL

    Returns:
        Success message
    """
    service = NotificationService(int(user_id))
    removed = await service.remove_subscription(endpoint)

    if removed:
        return {"success": True, "message": "Subscription removed"}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found"
        )


@router.get("/devices", response_model=DevicesResponse)
async def list_devices(user_id: str = Depends(require_auth)):
    """
    List all devices with push notifications enabled.

    Returns:
        DevicesResponse with list of devices
    """
    try:
        logger.info(f"User ID in list_devices: {user_id}")
        service = NotificationService(int(user_id))
        subscriptions = await service.get_subscriptions()

        logger.info(f"Got subscriptions: {type(subscriptions)} - {subscriptions}")

        devices = []
        for sub in subscriptions:
            logger.info(f"Processing sub: {type(sub)} - {sub}")
            devices.append(DeviceInfo(
                id=sub["id"],
                device_name=sub["device_name"],
                created_at=sub["created_at"],
                last_used_at=sub["last_used_at"]
            ))

        return DevicesResponse(devices=devices, total=len(devices))
    except Exception as e:
        logger.error(f"Failed to list devices: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@router.delete("/devices/{device_id}")
async def remove_device(
    device_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Remove a device from push notifications.

    Args:
        device_id: The device subscription ID

    Returns:
        Success message
    """
    service = NotificationService(int(user_id))
    removed = await service.remove_subscription_by_id(device_id)

    if removed:
        return {"success": True, "message": "Device removed"}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )


class DeviceNameUpdate(BaseModel):
    """Request model for updating device name."""
    device_name: str


@router.patch("/devices/{device_id}")
async def update_device_name(
    device_id: int,
    update: DeviceNameUpdate,
    user_id: str = Depends(require_auth)
):
    """
    Update the name of a subscribed device.

    Args:
        device_id: The device subscription ID
        update: New device name

    Returns:
        Success message with updated name
    """
    if not update.device_name or not update.device_name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device name cannot be empty"
        )

    device_name = update.device_name.strip()[:50]  # Max 50 chars

    service = NotificationService(int(user_id))
    updated = await service.update_device_name(device_id, device_name)

    if updated:
        return {"success": True, "device_name": device_name}
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )


@router.post("/test", response_model=TestNotificationResponse)
async def send_test_notification(user_id: str = Depends(require_auth)):
    """
    Send a test notification to all user's devices.

    Returns:
        TestNotificationResponse with send results
    """
    try:
        service = NotificationService(int(user_id))
        result = await service.send_notification(
            title="🔔 Test Notification",
            body="Push notifications are working!",
            url="/"
        )

        if result["sent"] > 0:
            message = f"Test notification sent to {result['sent']} device(s)"
        elif result["errors"] and "No devices subscribed" in result["errors"]:
            message = "No devices subscribed. Enable notifications first."
        else:
            message = "Failed to send notification"

        return TestNotificationResponse(
            sent=result["sent"],
            failed=result["failed"],
            message=message
        )
    except Exception as e:
        logger.error(f"Failed to send test notification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# ============================================================================
# Timer Endpoints
# ============================================================================

@router.post("/timers", response_model=TimerResponse)
async def create_timer(
    timer: TimerRequest,
    user_id: str = Depends(require_auth)
):
    """
    Create a new timer.

    Args:
        timer: Timer duration and optional label

    Returns:
        TimerResponse with timer info
    """
    if timer.duration <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Timer duration must be positive"
        )

    if timer.duration > 86400 * 7:  # Max 7 days
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Timer duration cannot exceed 7 days"
        )

    service = NotificationService(int(user_id))
    result = await service.set_timer(
        duration_seconds=timer.duration,
        label=timer.label or "Timer"
    )

    return TimerResponse(**result)


@router.get("/timers", response_model=TimersListResponse)
async def list_timers(user_id: str = Depends(require_auth)):
    """
    List all active timers.

    Returns:
        TimersListResponse with list of timers and remaining time
    """
    service = NotificationService(int(user_id))
    timers = await service.get_active_timers()

    return TimersListResponse(
        timers=[TimerInfo(**t) for t in timers],
        total=len(timers)
    )


@router.delete("/timers/{timer_id}", response_model=CancelResponse)
async def cancel_timer(
    timer_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Cancel an active timer.

    Args:
        timer_id: The timer ID to cancel

    Returns:
        CancelResponse with success status
    """
    service = NotificationService(int(user_id))
    cancelled = await service.cancel_notification(timer_id)

    if cancelled:
        return CancelResponse(success=True, message="Timer cancelled")
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Timer not found or already completed"
        )


# ============================================================================
# Alarm Endpoints
# ============================================================================

@router.post("/alarms", response_model=AlarmResponse)
async def create_alarm(
    alarm: AlarmRequest,
    user_id: str = Depends(require_auth)
):
    """
    Create a new alarm.

    Args:
        alarm: Alarm time, label, and optional repeat pattern

    Returns:
        AlarmResponse with alarm info
    """
    try:
        alarm_time = datetime.fromisoformat(alarm.time.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid time format. Use ISO 8601 (e.g., 2026-01-21T07:00:00)"
        )

    # Validate repeat pattern
    valid_patterns = [None, "daily", "weekdays", "weekly"]
    if alarm.repeat not in valid_patterns:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid repeat pattern. Use: daily, weekdays, or weekly"
        )

    service = NotificationService(int(user_id))
    result = await service.set_alarm(
        alarm_time=alarm_time,
        label=alarm.label or "Alarm",
        repeat_pattern=alarm.repeat
    )

    return AlarmResponse(**result)


@router.get("/alarms", response_model=AlarmsListResponse)
async def list_alarms(user_id: str = Depends(require_auth)):
    """
    List all active alarms.

    Returns:
        AlarmsListResponse with list of alarms
    """
    service = NotificationService(int(user_id))
    alarms = await service.get_active_alarms()

    return AlarmsListResponse(
        alarms=[AlarmInfo(**a) for a in alarms],
        total=len(alarms)
    )


@router.delete("/alarms/{alarm_id}", response_model=CancelResponse)
async def cancel_alarm(
    alarm_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Cancel an alarm.

    Args:
        alarm_id: The alarm ID to cancel

    Returns:
        CancelResponse with success status
    """
    service = NotificationService(int(user_id))
    cancelled = await service.cancel_notification(alarm_id)

    if cancelled:
        return CancelResponse(success=True, message="Alarm cancelled")
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alarm not found"
        )
