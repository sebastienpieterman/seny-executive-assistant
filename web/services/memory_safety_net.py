"""
Memory Safety Net — Phase 60-03

Post-response fallback for missed seny_remember/seny_update_memory calls.
Runs after every Telegram/Slack bot response. If the user's message looks
like a behavioral correction and Claude didn't save anything, auto-extracts
the rule and saves it, then returns the saved text for footnote appending.

Design: fail-open. Any exception returns None (no save, no footnote).
"""

import logging
import re
from typing import Optional

from anthropic import AsyncAnthropic
from src.core.config import Config
from web.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Keyword pre-filter — cheap check before spending a Haiku call
CORRECTION_SIGNALS = re.compile(
    r"\b(don'?t|stop|never|not like that|that'?s not|you keep|wrong|instead|"
    r"actually|quit|avoid|shouldn'?t|no more|cut it out|drop the|leave out|"
    r"skip the|that wasn'?t|i said|i told you|didn'?t i say|remember when)\b",
    re.IGNORECASE
)


async def check_and_save_missed_correction(
    user_message: str,
    memory_was_saved: bool,
    user_id: int,
) -> Optional[str]:
    """
    If the user's message looks like a correction and no memory was saved,
    auto-extract the behavioral rule via Haiku and save it.

    Returns the saved memory text (for footnote) or None if nothing was saved.
    """
    if memory_was_saved:
        return None

    # Cheap pre-filter first — skip Haiku call if no correction signals
    if not CORRECTION_SIGNALS.search(user_message):
        return None

    try:
        client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)

        # Step 1: Is this actually a behavioral correction?
        detection_response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    f"Is this message a behavioral correction telling an AI assistant "
                    f"to do something differently in future conversations?\n\n"
                    f"Message: \"{user_message}\"\n\n"
                    f"Answer only: yes or no"
                )
            }]
        )
        answer = detection_response.content[0].text.strip().lower()
        if not answer.startswith("yes"):
            return None

        # Step 2: Extract the behavioral rule
        extraction_response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"Extract the behavioral rule from this correction as a single "
                    f"specific instruction for an AI assistant. Write it as an instruction "
                    f"('Do X' or 'Don't do Y when Z'), not a description of what the user said. "
                    f"One sentence only, no preamble.\n\n"
                    f"Correction: \"{user_message}\""
                )
            }]
        )
        rule = extraction_response.content[0].text.strip()

        # Strip markdown fences if present
        if rule.startswith("```"):
            lines = rule.split("\n")
            rule = "\n".join(l for l in lines if not l.startswith("```")).strip()

        if not rule:
            return None

        # Save to user_memories
        MemoryService.save_memory(user_id, rule, category='behavior')
        logger.info(
            "[memory_safety_net] auto-saved missed correction for user %d: %s",
            user_id, rule[:80]
        )
        return rule

    except Exception as e:
        logger.warning("[memory_safety_net] failed for user %d: %s", user_id, repr(e))
        return None
