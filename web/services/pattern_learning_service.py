"""
Pattern Learning Service - Analyzes user feedback to personalize nudge behavior.

Phase 17-03: User Pattern Learning

Computes user patterns from feedback history:
- Responsive hours: When user typically engages with nudges
- Item type preferences: What kinds of nudges user finds helpful vs annoying
- Urgency adjustments: How to tune urgency classification per item type

Usage:
    service = PatternLearningService(user_id)
    patterns = await service.compute_patterns()
    responsive = await service.get_responsive_hours()
    should_skip = await service.should_suppress_item_type('needs_reply')
"""

import json
import logging
from typing import Optional

from web.core.database import (
    get_db,
    get_pattern_preferences,
    get_suppression_overrides,
    update_pattern_preferences,
)

logger = logging.getLogger(__name__)


class PatternLearningService:
    """
    Analyzes user feedback to learn preferences and personalize nudge behavior.

    Follows per-user service pattern. Computes patterns from feedback history
    and stores them for fast retrieval during nudge processing.
    """

    def __init__(self, user_id: int):
        """
        Initialize PatternLearningService for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id
        self._cached_preferences: Optional[dict] = None

    async def compute_patterns(self) -> dict:
        """
        Analyze feedback history and compute user patterns.

        Computes:
        - responsive_hours: Hours (0-23) when user engages with nudges
        - item_type_preferences: Preference scores by item type (-1.0 to 1.0)
        - preferred_channels_by_time: (Future) Channel preferences by time of day

        Stores computed patterns in user_pattern_preferences table.

        Returns:
            Dict with responsive_hours, item_type_preferences, channel_preferences
        """
        patterns = {
            'responsive_hours': [],
            'item_type_preferences': {},
            'channel_preferences': {},  # Future - for now empty
        }

        try:
            # Compute responsive hours
            patterns['responsive_hours'] = await self._compute_responsive_hours()

            # Compute item type preferences
            patterns['item_type_preferences'] = await self._compute_item_type_preferences()

            # Compute lessons learned from feedback reasons
            lessons_learned = await self._compute_lessons_learned()

            # Store computed patterns
            success = update_pattern_preferences(
                user_id=self.user_id,
                responsive_hours=json.dumps(patterns['responsive_hours']),
                item_type_preferences=json.dumps(patterns['item_type_preferences']),
                preferred_channels_by_time=json.dumps(patterns['channel_preferences']),
                lessons_learned=json.dumps(lessons_learned),
            )

            if success:
                logger.info(
                    "Computed patterns for user %d: %d responsive hours, %d item types, %d lesson types",
                    self.user_id,
                    len(patterns['responsive_hours']),
                    len(patterns['item_type_preferences']),
                    len(lessons_learned),
                )
                # Clear cache so next read gets fresh data
                self._cached_preferences = None
            else:
                logger.warning("Failed to store computed patterns for user %d", self.user_id)

        except Exception as e:
            logger.error("Error computing patterns for user %d: %s", self.user_id, repr(e))

        return patterns

    async def _compute_responsive_hours(self) -> list[int]:
        """
        Compute hours when user is most responsive to nudges.

        Based on: when user marks nudges 'helpful' or when they act on items.
        Returns top 6 hours with most engagement.

        Returns:
            List of hours (0-23) when user typically engages
        """
        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Query nudges where user responded positively
                # Extract hour from sent_at, count occurrences
                cursor.execute("""
                    SELECT EXTRACT(HOUR FROM sent_at)::INTEGER as hour,
                           COUNT(*) as count
                    FROM nudges
                    WHERE user_id = %s
                      AND sent_at IS NOT NULL
                      AND user_response IN ('helpful', 'acted')
                    GROUP BY hour
                    ORDER BY count DESC
                    LIMIT 6
                """, (self.user_id,))

                rows = cursor.fetchall()

                if not rows:
                    # No feedback yet - return default hours (9am-6pm)
                    return [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]

                return [row['hour'] for row in rows]

        except Exception as e:
            logger.error("Error computing responsive hours: %s", repr(e))
            return [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]

    async def _compute_item_type_preferences(self) -> dict[str, float]:
        """
        Compute preference scores by item type.

        For each item_type, calculates:
        (helpful_count - not_helpful_count) / total_count

        Normalizes to range -1.0 to 1.0:
        - Positive = user wants more of this type
        - Negative = user wants less of this type

        Returns:
            Dict mapping item_type to preference score
        """
        preferences = {}

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                # Get feedback counts by item_type and feedback_type
                cursor.execute("""
                    SELECT item_type, feedback_type, COUNT(*) as count
                    FROM user_feedback
                    WHERE user_id = %s
                    GROUP BY item_type, feedback_type
                """, (self.user_id,))

                rows = cursor.fetchall()

                if not rows:
                    return preferences

                # Aggregate by item_type
                type_counts: dict[str, dict[str, int]] = {}
                for row in rows:
                    item_type = row['item_type']
                    feedback_type = row['feedback_type']
                    count = row['count']

                    if item_type not in type_counts:
                        type_counts[item_type] = {'helpful': 0, 'not_helpful': 0, 'total': 0}

                    type_counts[item_type]['total'] += count

                    # Map feedback types to helpful/not_helpful
                    if feedback_type in ('helpful', 'more_like_this', 'accurate'):
                        type_counts[item_type]['helpful'] += count
                    elif feedback_type in ('not_helpful', 'less_like_this', 'inaccurate', 'too_much'):
                        type_counts[item_type]['not_helpful'] += count
                    # 'snooze' is neutral, doesn't affect preference

                # Calculate normalized preferences
                for item_type, counts in type_counts.items():
                    total = counts['total']
                    if total == 0:
                        continue

                    helpful = counts['helpful']
                    not_helpful = counts['not_helpful']

                    # Score = (helpful - not_helpful) / total
                    # Range: -1.0 to 1.0
                    score = (helpful - not_helpful) / total
                    preferences[item_type] = round(score, 2)

        except Exception as e:
            logger.error("Error computing item type preferences: %s", repr(e))

        return preferences

    async def _compute_lessons_learned(self) -> dict[str, list[str]]:
        """
        Aggregate non-null feedback reasons from the last 30 days by feedback_type.

        Groups up to 5 unique reasons per feedback_type to form a concise
        lessons-learned summary. This helps Seny understand *why* feedback
        was given, not just what type.

        Returns:
            Dict mapping feedback_type -> list of unique reason strings (max 5 each)
        """
        lessons: dict[str, list[str]] = {}

        try:
            with get_db() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT feedback_type, reason
                    FROM user_feedback
                    WHERE user_id = %s
                      AND reason IS NOT NULL
                      AND reason != ''
                      AND created_at >= NOW() - INTERVAL '30 days'
                    ORDER BY created_at DESC
                """, (self.user_id,))

                rows = cursor.fetchall()

                for row in rows:
                    feedback_type = row['feedback_type']
                    reason = row['reason'].strip()

                    if not reason:
                        continue

                    if feedback_type not in lessons:
                        lessons[feedback_type] = []

                    # Cap at 5 unique reasons per type
                    if reason not in lessons[feedback_type] and len(lessons[feedback_type]) < 5:
                        lessons[feedback_type].append(reason)

        except Exception as e:
            logger.error("Error computing lessons learned for user %d: %s", self.user_id, repr(e))

        return lessons

    def get_lessons_learned(self) -> dict:
        """
        Return aggregated feedback reasons grouped by feedback_type.

        Reads from cached/stored preferences. Returns empty dict if
        no lessons have been computed yet.

        Returns:
            Dict mapping feedback_type -> list of reason strings
        """
        prefs = self._get_cached_preferences()

        if prefs and prefs.get('lessons_learned'):
            try:
                lessons = prefs['lessons_learned']
                if isinstance(lessons, str):
                    lessons = json.loads(lessons)
                return lessons
            except (json.JSONDecodeError, TypeError):
                pass

        return {}

    async def get_responsive_hours(self) -> list[int]:
        """
        Return hours (0-23) when user is most responsive to nudges.

        Reads from cached/stored preferences. If not computed yet,
        returns default hours.

        Returns:
            List of hours when user typically engages
        """
        prefs = self._get_cached_preferences()

        if prefs and prefs.get('responsive_hours'):
            try:
                hours = prefs['responsive_hours']
                if isinstance(hours, str):
                    hours = json.loads(hours)
                return hours
            except (json.JSONDecodeError, TypeError):
                pass

        # Default: 9am-6pm
        return [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]

    async def get_item_type_preferences(self) -> dict[str, float]:
        """
        Return preference scores by item type (-1.0 to 1.0).

        Positive = user wants more of this type
        Negative = user wants less of this type

        Returns:
            Dict mapping item_type to preference score
        """
        prefs = self._get_cached_preferences()

        if prefs and prefs.get('item_type_preferences'):
            try:
                preferences = prefs['item_type_preferences']
                if isinstance(preferences, str):
                    preferences = json.loads(preferences)
                return preferences
            except (json.JSONDecodeError, TypeError):
                pass

        return {}

    async def should_suppress_item_type(self, item_type: str) -> bool:
        """
        Return True if user has shown strong negative preference for item type.

        Checks suppression overrides first — if an override is active for this
        item_type (override value is True), suppression is skipped unconditionally,
        regardless of computed preference score. This ensures the compute cycle
        cannot reinstate suppression after the user has explicitly reset it.

        Suppresses if preference score < -0.5 (user consistently dislikes this type)
        and no override is active.

        Args:
            item_type: Type of nudge/item to check

        Returns:
            True if should suppress, False otherwise
        """
        # Check overrides first — override wins unconditionally over computed score
        overrides = get_suppression_overrides(self.user_id)
        if overrides.get(item_type) is True:
            logger.debug(
                "Override active for %s user %d, skipping suppression",
                item_type, self.user_id
            )
            return False

        preferences = await self.get_item_type_preferences()
        score = preferences.get(item_type, 0.0)

        if score < -0.5:
            logger.debug(
                "Suppressing %s for user %d (preference score: %.2f)",
                item_type, self.user_id, score
            )
            return True

        return False

    async def get_urgency_adjustment(self, item_type: str) -> float:
        """
        Return multiplier for urgency scoring (0.5 to 1.5).

        Based on user's preference for this item type:
        - If user frequently dismisses: lower urgency (0.5-0.9)
        - If user frequently marks helpful: higher urgency (1.1-1.5)
        - Neutral: no adjustment (1.0)

        Args:
            item_type: Type of nudge/item to get adjustment for

        Returns:
            Multiplier for urgency scoring (0.5 to 1.5)
        """
        preferences = await self.get_item_type_preferences()
        score = preferences.get(item_type, 0.0)

        # Map preference score (-1.0 to 1.0) to adjustment (0.5 to 1.5)
        # score -1.0 -> adjustment 0.5
        # score  0.0 -> adjustment 1.0
        # score  1.0 -> adjustment 1.5
        adjustment = 1.0 + (score * 0.5)

        # Clamp to range [0.5, 1.5]
        adjustment = max(0.5, min(1.5, adjustment))

        return round(adjustment, 2)

    def _get_cached_preferences(self) -> Optional[dict]:
        """
        Get preferences with caching to avoid repeated DB queries.

        Returns:
            Cached or freshly loaded preferences dict
        """
        if self._cached_preferences is None:
            self._cached_preferences = get_pattern_preferences(self.user_id)
        return self._cached_preferences
