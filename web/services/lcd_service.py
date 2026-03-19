import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from web.core.database import (
    get_lcd_layer1, get_recent_lcd_observations, set_lcd_synthesis,
    get_user_profile
)

logger = logging.getLogger(__name__)

SYNTHESIS_TTL_HOURS = 2


class LCDService:
    def __init__(self, user_id: int):
        self.user_id = user_id

    async def get_lcd_context_for_injection(self) -> Optional[str]:
        """
        Returns the formatted LCD context block for system prompt injection.
        Returns None if Layer 1 is empty or on any error — always fail-open.
        Chat must never fail because of LCD.
        """
        try:
            record = get_lcd_layer1(self.user_id)
            layer1 = record["content"].strip() if record else ""
            layer2 = record.get("layer2_synthesis", "").strip() if record else ""
            synthesized_at = record.get("layer2_synthesized_at") if record else None

            # Check if synthesis needs refreshing
            observations = get_recent_lcd_observations(self.user_id, limit=20)
            if observations:
                if synthesized_at and isinstance(synthesized_at, str):
                    synthesized_at = datetime.fromisoformat(synthesized_at.replace('Z', '+00:00'))
                stale = (
                    not synthesized_at
                    or datetime.now(timezone.utc) - synthesized_at.replace(tzinfo=timezone.utc)
                    > timedelta(hours=SYNTHESIS_TTL_HOURS)
                )
                if stale:
                    fresh_synthesis = await self._synthesize_layer2(observations)
                    print(f"[LCD] synthesis result={bool(fresh_synthesis)}")
                    if fresh_synthesis:
                        layer2 = fresh_synthesis
                        set_lcd_synthesis(self.user_id, layer2)

            # Return None only if there's truly nothing to inject
            if not layer1 and not layer2:
                return None

            # Build context block
            profile = get_user_profile(self.user_id)
            user_name = profile['user_name']
            parts = []
            if layer1:
                parts += [f"**Who {user_name} Is:**", layer1]
            if layer2:
                parts += ["", "**What's Going On Right Now:**", layer2]

            return "\n".join(parts)

        except Exception as e:
            logger.warning("LCD injection failed (non-blocking): %s", repr(e))
            return None  # Fail-open — chat continues without LCD

    async def _get_layer2_for_context(self) -> Optional[str]:
        """Return current Layer 2 synthesis text, triggering re-synthesis if stale."""
        try:
            record = get_lcd_layer1(self.user_id)
            layer2 = record.get("layer2_synthesis", "").strip() if record else ""
            synthesized_at = record.get("layer2_synthesized_at") if record else None
            observations = get_recent_lcd_observations(self.user_id, limit=20)
            if observations:
                if synthesized_at and isinstance(synthesized_at, str):
                    synthesized_at = datetime.fromisoformat(synthesized_at.replace('Z', '+00:00'))
                stale = (
                    not synthesized_at
                    or datetime.now(timezone.utc) - synthesized_at.replace(tzinfo=timezone.utc)
                    > timedelta(hours=SYNTHESIS_TTL_HOURS)
                )
                if stale:
                    fresh = await self._synthesize_layer2(observations)
                    if fresh:
                        layer2 = fresh
                        set_lcd_synthesis(self.user_id, layer2)
            return layer2 or None
        except Exception as e:
            logger.warning("LCD _get_layer2_for_context failed: %s", repr(e))
            return None

    async def _synthesize_layer2(self, observations: list) -> Optional[str]:
        """
        Call Claude Haiku to synthesize recent observations into a 3-5 sentence paragraph.
        Returns None on any failure — synthesis is best-effort.
        """
        try:
            import anthropic
            # Uses AsyncAnthropic — same pattern as claude_service.py
            client = anthropic.AsyncAnthropic()

            profile = get_user_profile(self.user_id)
            user_name = profile['user_name']

            obs_text = "\n".join(
                f"[{o.get('source', 'unknown')} — {o.get('created_at', '')}] {o.get('content', '')}"
                for o in observations
            )

            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=(
                    "You are a summarization engine. Your only job is to distill observations into a "
                    "concise present-tense paragraph. Never ask for more information. Never say you "
                    "lack context. Work only with what is provided, even if it is a single observation. "
                    "Output the paragraph directly — no preamble, no header, no questions."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Summarize what is currently going on with {user_name} based on these observations. "
                        "Write 1-3 sentences in present tense. Be specific — name the actual things. "
                        "If there is only one observation, write one sentence about it.\n\n"
                        f"{obs_text}"
                    )
                }]
            )
            return response.content[0].text.strip()

        except Exception as e:
            logger.warning("LCD Layer 2 synthesis failed: %s", repr(e))
            return None
