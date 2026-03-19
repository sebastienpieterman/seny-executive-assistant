"""
Notification Service for Seny - Phase 7 (07-09)

Provides push notification management:
- Web Push subscription storage and delivery
- Timer and alarm scheduling
- Task reminder notification delivery

Usage:
    notification = NotificationService(user_id)
    await notification.save_subscription(endpoint, p256dh, auth)
    await notification.send_notification("Timer Done!", "Your 10-minute timer is up.")
    result = await notification.set_timer(600, "Pasta timer")
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from pywebpush import webpush, WebPushException
from web.core.database import get_db

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Push notification management service.

    Handles Web Push subscriptions and scheduled notifications.
    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize Notification service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id
        self.vapid_private_key = os.getenv("VAPID_PRIVATE_KEY")
        self.vapid_public_key = os.getenv("VAPID_PUBLIC_KEY")
        self.vapid_email = os.getenv("VAPID_EMAIL", "mailto:noreply@example.com")

    # =========================================================================
    # Subscription Management
    # =========================================================================

    async def save_subscription(
        self,
        endpoint: str,
        p256dh_key: str,
        auth_key: str,
        user_agent: str = None
    ) -> dict:
        """
        Save a new push subscription for this user.

        Args:
            endpoint: Push service endpoint URL
            p256dh_key: Browser public key (base64)
            auth_key: Auth secret (base64)
            user_agent: Browser/device user agent string

        Returns:
            Dict with subscription id and device_name
        """
        # Generate friendly device name from user agent
        device_name = self._parse_device_name(user_agent) if user_agent else "Unknown device"

        with get_db() as db:
            cursor = db.cursor()

            # Upsert - update if endpoint exists, insert if not
            cursor.execute("""
                INSERT INTO push_subscriptions
                (user_id, endpoint, p256dh_key, auth_key, user_agent, device_name, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(user_id, endpoint) DO UPDATE SET
                    p256dh_key = excluded.p256dh_key,
                    auth_key = excluded.auth_key,
                    user_agent = excluded.user_agent,
                    device_name = excluded.device_name,
                    last_used_at = %s
                RETURNING id
            """, (
                self.user_id, endpoint, p256dh_key, auth_key,
                user_agent, device_name, datetime.utcnow().isoformat(),
                datetime.utcnow().isoformat()
            ))

            subscription_id = cursor.fetchone()['id']

        logger.info(f"Saved push subscription {subscription_id} for user {self.user_id}: {device_name}")

        return {
            "id": subscription_id,
            "device_name": device_name
        }

    async def get_subscriptions(self) -> list[dict]:
        """
        Get all active push subscriptions for this user.

        Returns:
            List of subscription dicts with id, device_name, created_at
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                SELECT id, endpoint, p256dh_key, auth_key, device_name, created_at, last_used_at
                FROM push_subscriptions
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (self.user_id,))


            rows = cursor.fetchall()

            return [
                {
                    "id": row["id"],
                    "endpoint": row["endpoint"],
                    "p256dh_key": row["p256dh_key"],
                    "auth_key": row["auth_key"],
                    "device_name": row["device_name"],
                    "created_at": row["created_at"],
                    "last_used_at": row["last_used_at"]
                }
                for row in rows
            ]

    async def remove_subscription(self, endpoint: str) -> bool:
        """
        Remove a push subscription by endpoint.

        Args:
            endpoint: The push service endpoint URL to remove

        Returns:
            True if subscription was removed, False if not found
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                DELETE FROM push_subscriptions
                WHERE user_id = %s AND endpoint = %s
            """, (self.user_id, endpoint))

            removed = cursor.rowcount > 0

        if removed:
            logger.info(f"Removed push subscription for user {self.user_id}")
        return removed

    async def remove_subscription_by_id(self, subscription_id: int) -> bool:
        """
        Remove a push subscription by ID.

        Args:
            subscription_id: The subscription ID to remove

        Returns:
            True if subscription was removed, False if not found
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                DELETE FROM push_subscriptions
                WHERE id = %s AND user_id = %s
            """, (subscription_id, self.user_id))

            return cursor.rowcount > 0

    async def update_device_name(self, subscription_id: int, device_name: str) -> bool:
        """
        Update the device name for a subscription.

        Args:
            subscription_id: The subscription ID to update
            device_name: New device name

        Returns:
            True if updated, False if not found
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                UPDATE push_subscriptions
                SET device_name = %s
                WHERE id = %s AND user_id = %s
            """, (device_name, subscription_id, self.user_id))

            updated = cursor.rowcount > 0

        if updated:
            logger.info(f"Updated device name to '{device_name}' for subscription {subscription_id}")
        return updated

    # =========================================================================
    # Send Notifications
    # =========================================================================

    async def send_notification(
        self,
        title: str,
        body: str = None,
        url: str = None,
        notification_type: str = "general",
        actions: list = None
    ) -> dict:
        """
        Send push notification to all user's subscribed devices.

        Args:
            title: Notification title
            body: Notification body text
            url: URL to open when notification is clicked
            notification_type: Type of notification (timer, alarm, reminder, general)
            actions: Optional list of action buttons

        Returns:
            Dict with sent count, failed count, and any errors
        """
        if not self.vapid_private_key:
            logger.error("VAPID_PRIVATE_KEY not configured - cannot send notifications")
            return {"sent": 0, "failed": 0, "errors": ["VAPID keys not configured"]}

        subscriptions = await self.get_subscriptions()
        if not subscriptions:
            logger.info(f"No push subscriptions for user {self.user_id}")
            return {"sent": 0, "failed": 0, "errors": ["No devices subscribed"]}

        payload = {
            "title": title,
            "body": body,
            "url": url or "/",
            "type": notification_type,
            "timestamp": datetime.utcnow().isoformat()
        }
        if actions:
            payload["actions"] = actions

        sent = 0
        failed = 0
        errors = []

        for sub in subscriptions:
            success = await self._send_to_subscription(sub, payload)
            if success:
                sent += 1
                # Update last_used_at
                with get_db() as db:
                    db.cursor().execute(
                        "UPDATE push_subscriptions SET last_used_at = %s WHERE id = %s",
                        (datetime.utcnow().isoformat(), sub["id"])
                    )
            else:
                failed += 1
                errors.append(f"Failed to send to {sub['device_name']}")

        logger.info(f"Sent notification to user {self.user_id}: {sent} sent, {failed} failed")
        return {"sent": sent, "failed": failed, "errors": errors}

    async def _send_to_subscription(self, subscription: dict, payload: dict) -> bool:
        """
        Send notification to a single subscription.

        Args:
            subscription: Subscription dict with endpoint, p256dh_key, auth_key
            payload: Notification payload dict

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            subscription_info = {
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh_key"],
                    "auth": subscription["auth_key"]
                }
            }

            webpush(
                subscription_info,
                data=json.dumps(payload),
                vapid_private_key=self.vapid_private_key,
                vapid_claims={
                    "sub": self.vapid_email
                }
            )
            return True

        except WebPushException as e:
            logger.error(f"WebPush error for subscription {subscription['id']}: {e}")

            # If subscription is expired/invalid, remove it
            if e.response and e.response.status_code in (404, 410):
                logger.info(f"Removing expired subscription {subscription['id']}")
                await self.remove_subscription_by_id(subscription["id"])

            return False
        except Exception as e:
            logger.error(f"Error sending to subscription {subscription['id']}: {e}")
            return False

    # =========================================================================
    # Scheduled Notifications
    # =========================================================================

    async def schedule_notification(
        self,
        title: str,
        body: str,
        scheduled_for: datetime,
        notification_type: str,
        url: str = None,
        repeat_pattern: str = None,
        repeat_until: datetime = None,
        task_id: int = None,
        conversation_id: str = None,
        timezone: str = "UTC"
    ) -> dict:
        """
        Schedule a notification for future delivery.

        Args:
            title: Notification title
            body: Notification body text
            scheduled_for: When to send the notification (UTC)
            notification_type: Type of notification (timer, alarm, reminder, task_reminder)
            url: URL to open when clicked
            repeat_pattern: Repeat pattern (daily, weekdays, weekly, or None)
            repeat_until: Stop repeating after this date
            task_id: Associated task ID (for task reminders)
            conversation_id: Which conversation created this
            timezone: User's timezone (for display purposes)

        Returns:
            Dict with notification id and fires_at timestamp
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                INSERT INTO scheduled_notifications
                (user_id, title, body, url, type, scheduled_for, timezone,
                 repeat_pattern, repeat_until, task_id, conversation_id, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
            """, (
                self.user_id, title, body, url, notification_type,
                scheduled_for.isoformat(), timezone,
                repeat_pattern, repeat_until.isoformat() if repeat_until else None,
                task_id, conversation_id, datetime.utcnow().isoformat()
            ))

            notification_id = cursor.fetchone()['id']

        logger.info(f"Scheduled notification {notification_id} for user {self.user_id} at {scheduled_for}")

        return {
            "id": notification_id,
            "fires_at": scheduled_for.isoformat(),
            "type": notification_type
        }

    async def cancel_notification(self, notification_id: int) -> bool:
        """
        Cancel a scheduled notification.

        Args:
            notification_id: The notification ID to cancel

        Returns:
            True if cancelled, False if not found
        """
        with get_db() as db:
            cursor = db.cursor()

            cursor.execute("""
                UPDATE scheduled_notifications
                SET status = 'cancelled'
                WHERE id = %s AND user_id = %s AND status = 'pending'
            """, (notification_id, self.user_id))

            cancelled = cursor.rowcount > 0

        if cancelled:
            logger.info(f"Cancelled notification {notification_id}")
        return cancelled

    async def get_scheduled(
        self,
        include_past: bool = False,
        notification_type: str = None,
        status: str = "pending"
    ) -> list[dict]:
        """
        Get scheduled notifications for this user.

        Args:
            include_past: Include past notifications
            notification_type: Filter by type (timer, alarm, reminder)
            status: Filter by status (pending, sent, cancelled, failed)

        Returns:
            List of notification dicts
        """
        with get_db() as db:
            cursor = db.cursor()

            query = """
                SELECT id, title, body, url, type, scheduled_for, timezone,
                       repeat_pattern, repeat_until, status, task_id, created_at
                FROM scheduled_notifications
                WHERE user_id = %s
            """
            params = [self.user_id]

            if not include_past:
                query += " AND (scheduled_for > %s OR status = 'pending')"
                params.append(datetime.utcnow().isoformat())

            if notification_type:
                query += " AND type = %s"
                params.append(notification_type)

            if status:
                query += " AND status = %s"
                params.append(status)

            query += " ORDER BY scheduled_for ASC"

            cursor.execute(query, params)


            rows = cursor.fetchall()

            return [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "body": row["body"],
                    "url": row["url"],
                    "type": row["type"],
                    "scheduled_for": row["scheduled_for"],
                    "timezone": row["timezone"],
                    "repeat_pattern": row["repeat_pattern"],
                    "repeat_until": row["repeat_until"],
                    "status": row["status"],
                    "task_id": row["task_id"],
                    "created_at": row["created_at"]
                }
                for row in rows
            ]

    # =========================================================================
    # Timer Helpers
    # =========================================================================

    async def set_timer(
        self,
        duration_seconds: int,
        label: str = "Timer",
        conversation_id: str = None
    ) -> dict:
        """
        Set a timer for X seconds from now.

        Args:
            duration_seconds: Duration in seconds
            label: Timer label
            conversation_id: Which conversation created this

        Returns:
            Dict with id, fires_at, label, remaining_seconds
        """
        fires_at = datetime.utcnow() + timedelta(seconds=duration_seconds)

        result = await self.schedule_notification(
            title=f"⏱️ {label}",
            body=f"Your {self._format_duration(duration_seconds)} timer is done!",
            scheduled_for=fires_at,
            notification_type="timer",
            url="/",
            conversation_id=conversation_id
        )

        return {
            "id": result["id"],
            "fires_at": fires_at.isoformat(),
            "label": label,
            "duration_seconds": duration_seconds
        }

    async def get_active_timers(self) -> list[dict]:
        """
        Get all active timers for this user.

        Returns:
            List of timer dicts with remaining time calculated
        """
        timers = await self.get_scheduled(notification_type="timer", status="pending")
        now = datetime.utcnow()

        result = []
        for timer in timers:
            fires_at = datetime.fromisoformat(timer["scheduled_for"])
            remaining = (fires_at - now).total_seconds()

            if remaining > 0:
                result.append({
                    "id": timer["id"],
                    "label": timer["title"].replace("⏱️ ", ""),
                    "fires_at": timer["scheduled_for"],
                    "remaining_seconds": int(remaining),
                    "remaining_formatted": self._format_duration(int(remaining))
                })

        return result

    # =========================================================================
    # Alarm Helpers
    # =========================================================================

    async def set_alarm(
        self,
        alarm_time: datetime,
        label: str = "Alarm",
        repeat_pattern: str = None,
        repeat_until: datetime = None,
        timezone: str = "UTC",
        conversation_id: str = None
    ) -> dict:
        """
        Set an alarm for a specific time.

        Args:
            alarm_time: When the alarm should fire (in user's timezone)
            label: Alarm label
            repeat_pattern: Repeat pattern (daily, weekdays, weekly)
            repeat_until: Stop repeating after this date
            timezone: User's timezone
            conversation_id: Which conversation created this

        Returns:
            Dict with id, fires_at, label, repeat info
        """
        repeat_desc = ""
        if repeat_pattern:
            repeat_desc = f" (repeats {repeat_pattern})"

        result = await self.schedule_notification(
            title=f"⏰ {label}",
            body=f"Alarm{repeat_desc}",
            scheduled_for=alarm_time,
            notification_type="alarm",
            url="/",
            repeat_pattern=repeat_pattern,
            repeat_until=repeat_until,
            timezone=timezone,
            conversation_id=conversation_id
        )

        return {
            "id": result["id"],
            "fires_at": alarm_time.isoformat(),
            "label": label,
            "repeat_pattern": repeat_pattern,
            "timezone": timezone
        }

    async def get_active_alarms(self) -> list[dict]:
        """
        Get all active alarms for this user.

        Returns:
            List of alarm dicts
        """
        alarms = await self.get_scheduled(notification_type="alarm", status="pending")

        return [
            {
                "id": alarm["id"],
                "label": alarm["title"].replace("⏰ ", ""),
                "fires_at": alarm["scheduled_for"],
                "repeat_pattern": alarm["repeat_pattern"],
                "repeat_until": alarm["repeat_until"],
                "timezone": alarm["timezone"]
            }
            for alarm in alarms
        ]

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def _format_duration(self, seconds: int) -> str:
        """Format seconds into human-readable duration."""
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if minutes:
                return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
            return f"{hours} hour{'s' if hours != 1 else ''}"

    def _parse_device_name(self, user_agent: str) -> str:
        """Parse a user-friendly device name from user agent string."""
        ua = user_agent.lower()

        # Detect browser
        if "firefox" in ua:
            browser = "Firefox"
        elif "edg" in ua:
            browser = "Edge"
        elif "chrome" in ua:
            browser = "Chrome"
        elif "safari" in ua:
            browser = "Safari"
        else:
            browser = "Browser"

        # Detect OS
        if "windows" in ua:
            os_name = "Windows"
        elif "mac" in ua:
            os_name = "Mac"
        elif "linux" in ua:
            os_name = "Linux"
        elif "android" in ua:
            os_name = "Android"
        elif "iphone" in ua or "ipad" in ua:
            os_name = "iOS"
        else:
            os_name = "Unknown"

        return f"{browser} on {os_name}"


def calculate_next_occurrence(current_time: datetime, repeat_pattern: str) -> Optional[datetime]:
    """
    Calculate the next occurrence for a repeating notification.

    Args:
        current_time: The current/last occurrence time
        repeat_pattern: Pattern string (daily, weekdays, weekly)

    Returns:
        Next occurrence datetime, or None if pattern is invalid
    """
    if repeat_pattern == "daily":
        return current_time + timedelta(days=1)

    elif repeat_pattern == "weekly":
        return current_time + timedelta(weeks=1)

    elif repeat_pattern == "weekdays":
        # Find next weekday (Mon-Fri)
        next_day = current_time + timedelta(days=1)
        while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
            next_day += timedelta(days=1)
        return next_day

    return None
