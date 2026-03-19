"""
NightlyResearchService - Phase 60

Runs at 4am to audit the feedback absorption chain.
Measures two concrete gaps:
  1. Negative feedback with reasons that didn't result in new memories (unabsorbed)
  2. Nudge types with repeated dismissals that pattern learning hasn't suppressed yet (suppression gap)

Saves results to research_audit_runs for Phase 61 experiment tracking.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from anthropic import AsyncAnthropic
from src.core.config import Config
from web.core.database import get_db, create_audit_run, get_last_audit_run, create_pending_action, get_pending_action_by_source_ref

logger = logging.getLogger(__name__)

NEGATIVE_FEEDBACK_TYPES = {'not_helpful', 'less_like_this', 'inaccurate', 'too_much'}
SUPPRESSION_THRESHOLD = -0.5
SUPPRESSION_GAP_MIN_DISMISSALS = 3
SUPPRESSION_GAP_WINDOW_DAYS = 7


class NightlyResearchService:
    def __init__(self, user_id: int):
        self.user_id = user_id

    def _get_lookback_cutoff(self) -> datetime:
        """Return the datetime to use as the start of the audit window.

        If a prior run exists for this user, return its run_at timestamp so we
        don't double-count signals that were already processed.  On the first
        ever run, look back 48 hours.
        """
        prior = get_last_audit_run(self.user_id)
        if prior and prior.get('run_at'):
            run_at = prior['run_at']
            # PostgreSQL may return a string or a datetime; normalise to datetime.
            if isinstance(run_at, str):
                # Strip trailing timezone info / fractional seconds if present
                run_at = run_at.split('+')[0].split('.')[0]
                return datetime.fromisoformat(run_at)
            if isinstance(run_at, datetime):
                return run_at.replace(tzinfo=None)
        return datetime.utcnow() - timedelta(hours=48)

    async def _collect_feedback_signals(self, since: datetime) -> dict:
        """Query the DB for feedback signals since *since* and return as a plain dict."""

        negative_with_reason = []
        new_memories = []
        dismissal_spikes = []
        current_preferences = {}

        with get_db() as conn:
            cursor = conn.cursor()

            # 1. Negative feedback with a reason, created after the cutoff
            cursor.execute(
                """
                SELECT id, item_type, feedback_type, reason, created_at
                FROM user_feedback
                WHERE user_id = %s
                  AND created_at > %s
                  AND feedback_type = ANY(%s)
                  AND reason IS NOT NULL AND reason != ''
                ORDER BY created_at ASC
                """,
                (self.user_id, since, list(NEGATIVE_FEEDBACK_TYPES)),
            )
            for row in cursor.fetchall():
                negative_with_reason.append({
                    "id": row['id'],
                    "item_type": row['item_type'],
                    "feedback_type": row['feedback_type'],
                    "reason": row['reason'],
                    "created_at": str(row['created_at']),
                })

            # 2. New memories added since the cutoff
            cursor.execute(
                """
                SELECT id, category, created_at FROM user_memories
                WHERE user_id = %s AND created_at > %s
                ORDER BY created_at ASC
                """,
                (self.user_id, since.isoformat()),
            )
            for row in cursor.fetchall():
                new_memories.append({
                    "id": row['id'],
                    "category": row['category'],
                    "created_at": str(row['created_at']),
                })

            # 3. Nudge types with 3+ dismissals in the last SUPPRESSION_GAP_WINDOW_DAYS days
            cursor.execute(
                """
                SELECT nudge_type, COUNT(*) as dismissal_count
                FROM nudges
                WHERE user_id = %s
                  AND sent_at >= NOW() - INTERVAL '7 days'
                  AND user_response IN ('dismissed', 'not_helpful', 'too_much')
                GROUP BY nudge_type
                HAVING COUNT(*) >= 3
                """,
                (self.user_id,),
            )
            for row in cursor.fetchall():
                dismissal_spikes.append({
                    "nudge_type": row['nudge_type'],
                    "dismissal_count": int(row['dismissal_count']),
                })

            # 4. Current item_type_preferences (to check suppression gaps)
            cursor.execute(
                "SELECT item_type_preferences FROM user_pattern_preferences WHERE user_id = %s",
                (self.user_id,),
            )
            pref_row = cursor.fetchone()
            if pref_row and pref_row['item_type_preferences']:
                raw = pref_row['item_type_preferences']
                if isinstance(raw, str):
                    try:
                        current_preferences = json.loads(raw)
                    except Exception:
                        current_preferences = {}
                elif isinstance(raw, dict):
                    current_preferences = raw

        # 5. Already-handled nudge signals from last 30 days (closure pattern mining)
        already_handled_feedback = []
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT uf.feedback_type, uf.item_type, uf.item_id, uf.created_at,
                           n.nudge_type, n.title, n.body
                    FROM user_feedback uf
                    LEFT JOIN nudges n ON uf.item_type = 'nudge' AND uf.item_id = n.id
                    WHERE uf.user_id = %s
                      AND uf.feedback_type = 'already_handled'
                      AND uf.created_at >= NOW() - INTERVAL '30 days'
                    ORDER BY uf.created_at DESC
                    LIMIT 20
                """, (self.user_id,))
                for row in cursor.fetchall():
                    already_handled_feedback.append({
                        "nudge_type": row['nudge_type'],
                        "title": row['title'],
                        "body": row['body'],
                        "created_at": str(row['created_at']),
                    })
        except Exception as e:
            logger.warning(
                "[nightly_research] already_handled fetch failed for user %d: %s",
                self.user_id, repr(e),
            )

        return {
            "negative_with_reason": negative_with_reason,
            "new_memories": new_memories,
            "dismissal_spikes": dismissal_spikes,
            "current_preferences": current_preferences,
            "already_handled_feedback": already_handled_feedback,
            "since": since.isoformat(),
        }

    def _compute_fidelity_score(self, signals: dict) -> dict:
        """Compute the two gap metrics and derive a fidelity score."""

        negative_with_reason = signals.get("negative_with_reason", [])
        new_memories = signals.get("new_memories", [])
        dismissal_spikes = signals.get("dismissal_spikes", [])
        current_preferences = signals.get("current_preferences", {})

        # --- Metric 1: unabsorbed negative feedback ---
        # A signal is "absorbed" if at least one memory was added within 24h after it.
        negative_unabsorbed_count = 0
        for fb in negative_with_reason:
            try:
                fb_time_str = fb['created_at'].split('+')[0].split('.')[0]
                fb_time = datetime.fromisoformat(fb_time_str)
            except Exception:
                negative_unabsorbed_count += 1
                continue

            window_end = fb_time + timedelta(hours=24)
            absorbed = False
            for mem in new_memories:
                try:
                    mem_time_str = mem['created_at'].split('+')[0].split('.')[0]
                    mem_time = datetime.fromisoformat(mem_time_str)
                except Exception:
                    continue
                if fb_time < mem_time <= window_end:
                    absorbed = True
                    break
            if not absorbed:
                negative_unabsorbed_count += 1

        # --- Metric 2: suppression gaps ---
        # A gap exists when dismissal spikes haven't been reflected in pattern scores yet.
        suppression_gap_count = 0
        for spike in dismissal_spikes:
            nudge_type = spike['nudge_type']
            score = current_preferences.get(nudge_type, 0.0)
            if score >= SUPPRESSION_THRESHOLD:
                suppression_gap_count += 1

        # --- Fidelity score ---
        total_negative = len(negative_with_reason) + len(dismissal_spikes)
        fidelity_score = round(
            1.0 - min(1.0, (negative_unabsorbed_count + suppression_gap_count) / max(1, total_negative)),
            3,
        )

        return {
            "fidelity_score": fidelity_score,
            "negative_unabsorbed_count": negative_unabsorbed_count,
            "suppression_gap_count": suppression_gap_count,
            "total_negative_signals": total_negative,
        }

    async def _generate_proposals(self, signals: dict) -> int:
        """
        For each unabsorbed negative feedback signal that has a reason,
        call Haiku to extract a memory rule and queue a research_proposal
        pending_action. Returns the number of proposals created.
        Fails open — any exception logs a warning and returns 0.
        """
        try:
            proposal_count = 0
            client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)

            for fb in signals.get("negative_with_reason", []):
                feedback_id = fb.get("id")
                reason = (fb.get("reason") or "").strip()
                if not reason or not feedback_id:
                    continue

                source_ref = f"feedback:{feedback_id}"
                # Dedup — skip if a proposal already exists for this feedback item
                if get_pending_action_by_source_ref(self.user_id, source_ref):
                    continue

                # Haiku gate: is this actually a behavioral correction?
                gate = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Is this user feedback a behavioral correction telling an AI "
                            "assistant to do something differently in the future?\n\n"
                            f"Feedback: \"{reason}\"\n\n"
                            "Answer only: yes or no"
                        ),
                    }],
                )
                if not gate.content[0].text.strip().lower().startswith("yes"):
                    continue

                # Haiku extraction: pull out the specific rule
                extraction = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Extract a behavioral rule from this feedback as a single "
                            "instruction for an AI assistant. Write it as 'Do X' or "
                            "'Don't do Y'. One sentence only, no preamble.\n\n"
                            f"Feedback: \"{reason}\""
                        ),
                    }],
                )
                rule = extraction.content[0].text.strip()
                # Strip markdown fences if Haiku adds them
                if rule.startswith("```"):
                    rule = "\n".join(
                        line for line in rule.split("\n") if not line.startswith("```")
                    ).strip()
                if not rule:
                    continue

                # Build pending_action content
                content = json.dumps({
                    "memory_rule": rule,
                    "memory_category": "behavior",
                    "evidence": reason[:300],
                })
                title = f'Memory suggestion: "{rule}"'
                if len(title) > 120:
                    title = f'Memory suggestion: "{rule[:80]}..."'

                create_pending_action(
                    self.user_id,
                    "research_proposal",
                    title,
                    content,
                    source="nightly_research",
                    source_ref=source_ref,
                )
                proposal_count += 1
                logger.info(
                    "[nightly_research] queued memory proposal for user %d from feedback %d",
                    self.user_id,
                    feedback_id,
                )

            # --- Already Handled Nudge Pattern Mining ---
            # Only run if there are already_handled signals to analyze
            already_handled_feedback = signals.get("already_handled_feedback", [])
            if already_handled_feedback:
                already_handled_summary = "\n".join(
                    f"- [{row.get('nudge_type', 'unknown')}] {row.get('title', '(no title)')}"
                    for row in already_handled_feedback
                )

                pattern_prompt = f"""You are analyzing nudge feedback to identify patterns that could reduce false alarms.

## Already Handled Nudge Patterns (last 30 days)

The user marked these nudges as "Already Handled" — meaning the situation was
already resolved before the nudge fired:

{already_handled_summary}

If you see a pattern here (e.g. nudges about a specific person tend to resolve
quickly, or a specific nudge type is frequently pre-resolved), generate a memory
rule like:
- "Conversations with [Person] tend to resolve quickly — apply stricter closure criteria"
- "[Nudge type] situations for this user often self-resolve — be conservative"

Only generate these rules if there are 3 or more examples of the same pattern.
Do not generate rules from single data points.

If no clear pattern exists with 3+ examples, respond with: NO_PATTERN

If a pattern exists, respond with a single memory rule sentence. No preamble."""

                pattern_response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,
                    messages=[{"role": "user", "content": pattern_prompt}],
                )
                pattern_rule = pattern_response.content[0].text.strip()

                if pattern_rule and pattern_rule != "NO_PATTERN" and not pattern_rule.startswith("NO_PATTERN"):
                    source_ref = f"already_handled_pattern:{self.user_id}"
                    if not get_pending_action_by_source_ref(self.user_id, source_ref):
                        content = json.dumps({
                            "memory_rule": pattern_rule,
                            "memory_category": "behavior",
                            "evidence": f"Pattern from {len(already_handled_feedback)} already_handled nudges",
                        })
                        title = f'Closure pattern: "{pattern_rule}"'
                        if len(title) > 120:
                            title = f'Closure pattern: "{pattern_rule[:80]}..."'

                        create_pending_action(
                            self.user_id,
                            "research_proposal",
                            title,
                            content,
                            source="nightly_research",
                            source_ref=source_ref,
                        )
                        proposal_count += 1
                        logger.info(
                            "[nightly_research] queued closure pattern proposal for user %d (%d already_handled signals)",
                            self.user_id,
                            len(already_handled_feedback),
                        )

            return proposal_count

        except Exception as e:
            logger.warning(
                "[nightly_research] _generate_proposals failed for user %d: %s",
                self.user_id, repr(e),
            )
            return 0

    async def run_audit(self) -> dict:
        """Collect signals, score fidelity, persist the result, and return a summary dict."""
        try:
            since = self._get_lookback_cutoff()
            signals = await self._collect_feedback_signals(since)
            fidelity = self._compute_fidelity_score(signals)

            run_id = create_audit_run(
                user_id=self.user_id,
                fidelity_score=fidelity['fidelity_score'],
                negative_unabsorbed_count=fidelity['negative_unabsorbed_count'],
                suppression_gap_count=fidelity['suppression_gap_count'],
                signals_json=json.dumps(signals, default=str),
            )

            proposal_count = await self._generate_proposals(signals)

            logger.info(
                "[nightly_research] user %d — fidelity=%.3f, unabsorbed=%d, suppression_gaps=%d, run_id=%s, proposals=%d",
                self.user_id,
                fidelity['fidelity_score'],
                fidelity['negative_unabsorbed_count'],
                fidelity['suppression_gap_count'],
                run_id,
                proposal_count,
            )

            return {
                "user_id": self.user_id,
                "run_id": run_id,
                "fidelity": fidelity,
                "proposal_count": proposal_count,
                "signals_summary": {
                    "negative_with_reason_count": len(signals["negative_with_reason"]),
                    "new_memories_count": len(signals["new_memories"]),
                    "dismissal_spikes": signals["dismissal_spikes"],
                },
            }
        except Exception as e:
            logger.error("[nightly_research] run_audit failed for user %d: %s", self.user_id, repr(e))
            return {"user_id": self.user_id, "error": repr(e)}
