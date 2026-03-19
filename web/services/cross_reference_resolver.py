"""
Cross-Reference Resolver - Links classified items to People, Projects, Ideas, Tasks.

Takes classification output (from InboundClassifier) and resolves entity references
into cross_references table entries. This is what makes the system a "Second Brain" —
connecting an incoming Slack message to the person who sent it, the project it's about,
and the task it relates to.

Phase 14-03 - Inbound Classification & Cross-Referencing
"""

import json
import logging
from typing import Optional

from web.core.database import (
    get_db,
    get_people_by_user,
    get_projects_by_user,
    get_user_identifiers,
    insert_cross_reference,
    insert_detected_action,
    resolve_entity,
)

logger = logging.getLogger(__name__)


class CrossReferenceResolver:
    """Resolves entity references from classified items into cross_references table."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def resolve_item(self, scanned_item: dict, classification: dict) -> dict:
        """
        Resolve all entity references for a classified item.

        Args:
            scanned_item: row from scanned_items table
            classification: parsed classification result with relevance, extracted_entities,
                           extracted_actions, etc.

        Returns:
            {"people": N, "projects": N, "ideas": N, "tasks": N, "actions": N}
            counts of cross-refs and actions created.
        """
        item_id = scanned_item.get("id")
        source = scanned_item.get("source", "")

        # Parse source_metadata
        metadata = scanned_item.get("source_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        elif metadata is None:
            metadata = {}

        # Parse extracted_entities from classification
        entities = classification.get("extracted_entities")
        if isinstance(entities, str):
            try:
                entities = json.loads(entities)
            except (json.JSONDecodeError, TypeError):
                entities = {}
        elif entities is None:
            entities = {}

        # Parse extracted_actions from classification
        actions = classification.get("extracted_actions") or classification.get("actions", [])
        if isinstance(actions, str):
            try:
                actions = json.loads(actions)
            except (json.JSONDecodeError, TypeError):
                actions = []

        results = {
            "people": 0,
            "projects": 0,
            "ideas": 0,
            "tasks": 0,
            "actions": 0,
        }

        # Resolve each entity type independently — one failure doesn't block others
        try:
            results["people"] = await self._resolve_people(item_id, entities, metadata, source)
        except Exception as e:
            logger.warning("People resolution failed for item %d: %s", item_id, repr(e))

        try:
            results["projects"] = await self._resolve_projects(item_id, entities, classification)
        except Exception as e:
            logger.warning("Project resolution failed for item %d: %s", item_id, repr(e))

        try:
            results["ideas"] = await self._resolve_ideas(item_id, classification)
        except Exception as e:
            logger.warning("Idea resolution failed for item %d: %s", item_id, repr(e))

        try:
            results["tasks"] = await self._resolve_tasks(item_id, classification, actions)
        except Exception as e:
            logger.warning("Task resolution failed for item %d: %s", item_id, repr(e))

        try:
            results["actions"] = await self._resolve_detected_actions(item_id, actions)
        except Exception as e:
            logger.warning("Action detection failed for item %d: %s", item_id, repr(e))

        total = sum(results.values())
        if total > 0:
            logger.info(
                "Cross-referenced item %d (%s): %s",
                item_id, source,
                ", ".join(f"{k}={v}" for k, v in results.items() if v > 0),
            )

        return results

    # -------------------------------------------------------------------------
    # People resolution
    # -------------------------------------------------------------------------

    async def _resolve_people(
        self, scanned_item_id: int, entities: dict, metadata: dict, source: str
    ) -> int:
        """
        Resolve people references via two strategies:
        1. entity_mappings lookup for sender (email/Slack user_id/Telegram sender_id)
        2. Name matching for people_mentioned from classification
        """
        count = 0

        # Strategy 1: Resolve sender via entity_mappings
        sender_person_id = self._resolve_sender_via_entity_mappings(source, metadata)
        if sender_person_id:
            ref_id = insert_cross_reference(
                user_id=self.user_id,
                scanned_item_id=scanned_item_id,
                entity_type="person",
                entity_id=sender_person_id,
                relationship="from",
                confidence=1.0,
            )
            if ref_id is not None:
                count += 1

        # For calendar events, resolve attendees too
        if source == "calendar":
            attendees = metadata.get("attendees", [])
            for email in attendees:
                if not email or not isinstance(email, str):
                    continue
                mapping = resolve_entity(self.user_id, "calendar", email.lower())
                if not mapping:
                    # Also try gmail source (same email address)
                    mapping = resolve_entity(self.user_id, "gmail", email.lower())
                if mapping and mapping.get("person_id"):
                    ref_id = insert_cross_reference(
                        user_id=self.user_id,
                        scanned_item_id=scanned_item_id,
                        entity_type="person",
                        entity_id=mapping["person_id"],
                        relationship="mentioned",
                        confidence=mapping.get("confidence", 0.9),
                    )
                    if ref_id is not None:
                        count += 1

        # Strategy 2: Name matching for people_mentioned
        people_mentioned = entities.get("people_mentioned", [])
        if people_mentioned:
            people_db = get_people_by_user(self.user_id, limit=500)
            resolved_ids = {sender_person_id} if sender_person_id else set()

            for name in people_mentioned:
                if not name or not isinstance(name, str):
                    continue
                person_id = self._match_person_by_name(name, people_db)
                if person_id and person_id not in resolved_ids:
                    ref_id = insert_cross_reference(
                        user_id=self.user_id,
                        scanned_item_id=scanned_item_id,
                        entity_type="person",
                        entity_id=person_id,
                        relationship="mentioned",
                        confidence=0.8,
                    )
                    if ref_id is not None:
                        count += 1
                        resolved_ids.add(person_id)

        return count

    def _resolve_sender_via_entity_mappings(
        self, source: str, metadata: dict
    ) -> Optional[int]:
        """Look up sender identity in entity_mappings table."""
        identifier = None

        if source == "gmail":
            from_field = metadata.get("from", "")
            # Parse email from "Name <email>" format
            if "<" in from_field and ">" in from_field:
                identifier = from_field[from_field.index("<") + 1:from_field.index(">")].strip().lower()
            elif "@" in from_field:
                identifier = from_field.strip().lower()

        elif source == "slack":
            user_id = metadata.get("user_id", "")
            team_id = metadata.get("team_id", "")
            if user_id:
                identifier = f"{team_id}:{user_id}" if team_id else user_id

        elif source == "telegram":
            sender_id = metadata.get("sender_id")
            if sender_id:
                identifier = str(sender_id)

        if not identifier:
            return None

        # Calendar uses its own source or gmail source — handled separately
        mapping = resolve_entity(self.user_id, source, identifier)
        if mapping and mapping.get("person_id"):
            return mapping["person_id"]

        return None

    def _match_person_by_name(self, name: str, people: list[dict]) -> Optional[int]:
        """
        Match a name against People DB using exact case-insensitive matching only.
        Avoids fuzzy/substring to prevent false positives in cross-referencing.
        """
        name_lower = name.strip().lower()
        if not name_lower:
            return None

        for person in people:
            person_name = (person.get("name") or "").strip().lower()
            if not person_name:
                continue
            # Exact match
            if name_lower == person_name:
                return person["id"]
            # First name match (only if both sides are just a first name)
            name_first = name_lower.split()[0]
            person_first = person_name.split()[0]
            if len(name_first) >= 3 and name_first == person_first:
                return person["id"]

        return None

    # -------------------------------------------------------------------------
    # Project resolution
    # -------------------------------------------------------------------------

    async def _resolve_projects(
        self, scanned_item_id: int, entities: dict, classification: dict
    ) -> int:
        """
        Resolve project references via case-insensitive substring matching
        against active projects.
        """
        count = 0
        projects_related = entities.get("projects_related", [])
        summary = classification.get("summary", "")

        # Load active projects
        active_projects = get_projects_by_user(self.user_id, status="active", limit=100)
        if not active_projects:
            return 0

        matched_project_ids = set()

        # Match classification's project references
        for proj_name in projects_related:
            if not proj_name or not isinstance(proj_name, str):
                continue
            proj_lower = proj_name.strip().lower()
            for project in active_projects:
                db_name = (project.get("name") or "").strip().lower()
                if not db_name:
                    continue
                pid = project["id"]
                if pid in matched_project_ids:
                    continue
                # Case-insensitive substring match (either direction)
                if proj_lower in db_name or db_name in proj_lower:
                    confidence = 1.0 if proj_lower == db_name else 0.7
                    ref_id = insert_cross_reference(
                        user_id=self.user_id,
                        scanned_item_id=scanned_item_id,
                        entity_type="project",
                        entity_id=pid,
                        relationship="about",
                        confidence=confidence,
                    )
                    if ref_id is not None:
                        count += 1
                        matched_project_ids.add(pid)

        # Also check if any active project name appears in the summary
        if summary:
            summary_lower = summary.lower()
            for project in active_projects:
                pid = project["id"]
                if pid in matched_project_ids:
                    continue
                db_name = (project.get("name") or "").strip().lower()
                if db_name and len(db_name) >= 4 and db_name in summary_lower:
                    ref_id = insert_cross_reference(
                        user_id=self.user_id,
                        scanned_item_id=scanned_item_id,
                        entity_type="project",
                        entity_id=pid,
                        relationship="about",
                        confidence=0.6,
                    )
                    if ref_id is not None:
                        count += 1
                        matched_project_ids.add(pid)

        return count

    # -------------------------------------------------------------------------
    # Idea resolution
    # -------------------------------------------------------------------------

    async def _resolve_ideas(self, scanned_item_id: int, classification: dict) -> int:
        """
        Resolve idea references using FTS5 keyword search from classification summary.
        Only creates cross-references for strong FTS5 matches to avoid noise.
        """
        count = 0
        summary = classification.get("summary", "")
        if not summary:
            return 0

        # Extract keywords: use ideas_related from entities if available, else summary words
        entities = classification.get("extracted_entities")
        if isinstance(entities, str):
            try:
                entities = json.loads(entities)
            except (json.JSONDecodeError, TypeError):
                entities = {}
        elif entities is None:
            entities = {}

        ideas_related = entities.get("ideas_related", [])

        # Build FTS query from idea keywords or summary
        search_terms = []
        if ideas_related:
            search_terms = [kw.strip() for kw in ideas_related if kw and isinstance(kw, str)]

        if not search_terms:
            # Extract meaningful words from summary (skip short/common words)
            words = summary.split()
            search_terms = [w.strip(".,!?;:'\"") for w in words if len(w) >= 5][:5]

        if not search_terms:
            return 0

        # Build ILIKE pattern from search terms
        ilike_pattern = '%' + ' '.join(search_terms[:5]) + '%'

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT i.id, i.title
                    FROM ideas i
                    WHERE (i.title ILIKE %s OR i.content ILIKE %s) AND i.user_id = %s
                    LIMIT 3
                """, (ilike_pattern, ilike_pattern, self.user_id))

                for row in cursor.fetchall():
                    ref_id = insert_cross_reference(
                        user_id=self.user_id,
                        scanned_item_id=scanned_item_id,
                        entity_type="idea",
                        entity_id=row["id"],
                        relationship="about",
                        confidence=0.5,
                    )
                    if ref_id is not None:
                        count += 1
        except Exception as e:
            logger.warning("Ideas FTS search failed: %s", repr(e))

        return count

    # -------------------------------------------------------------------------
    # Task resolution
    # -------------------------------------------------------------------------

    async def _resolve_tasks(
        self, scanned_item_id: int, classification: dict, actions: list
    ) -> int:
        """
        Resolve task references via:
        1. Deadline matching against open tasks with due dates
        2. Title keyword matching against open task titles
        """
        count = 0

        # Collect deadlines from actions
        deadlines = []
        action_texts = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            dl = action.get("deadline")
            if dl:
                deadlines.append(dl)
            text = action.get("action", "")
            if text:
                action_texts.append(text)

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                matched_task_ids = set()

                # Strategy 1: Deadline matching
                for deadline in deadlines:
                    cursor.execute("""
                        SELECT id FROM tasks
                        WHERE user_id = %s AND status != 'completed' AND due_date = %s
                    """, (self.user_id, deadline))
                    for row in cursor.fetchall():
                        tid = row["id"]
                        if tid in matched_task_ids:
                            continue
                        ref_id = insert_cross_reference(
                            user_id=self.user_id,
                            scanned_item_id=scanned_item_id,
                            entity_type="task",
                            entity_id=tid,
                            relationship="deadline",
                            confidence=0.8,
                        )
                        if ref_id is not None:
                            count += 1
                            matched_task_ids.add(tid)

                # Strategy 2: Title keyword matching (LIKE-based since no tasks FTS)
                for action_text in action_texts:
                    # Extract meaningful keywords from action text
                    words = action_text.split()
                    keywords = [w.strip(".,!?;:'\"").lower() for w in words if len(w) >= 5][:3]
                    for keyword in keywords:
                        cursor.execute("""
                            SELECT id FROM tasks
                            WHERE user_id = %s AND status != 'completed'
                            AND title ILIKE %s
                            LIMIT 3
                        """, (self.user_id, f"%{keyword}%"))
                        for row in cursor.fetchall():
                            tid = row["id"]
                            if tid in matched_task_ids:
                                continue
                            ref_id = insert_cross_reference(
                                user_id=self.user_id,
                                scanned_item_id=scanned_item_id,
                                entity_type="task",
                                entity_id=tid,
                                relationship="about",
                                confidence=0.6,
                            )
                            if ref_id is not None:
                                count += 1
                                matched_task_ids.add(tid)

        except Exception as e:
            logger.warning("Task resolution failed: %s", repr(e))

        return count

    # -------------------------------------------------------------------------
    # Detected actions
    # -------------------------------------------------------------------------

    async def _resolve_detected_actions(
        self, scanned_item_id: int, actions: list
    ) -> int:
        """
        Store detected action items from classification, resolving person names
        to person_id where possible.
        """
        count = 0
        if not actions:
            return 0

        # HF-04: Load user identifiers once to filter self-referential action items
        user_identifiers = get_user_identifiers(self.user_id)
        # Build set of lowercased values to match against (names, email local parts, full emails)
        _self_names: set[str] = set()
        for name in user_identifiers.get('display_names', []):
            if name:
                _self_names.add(name.strip().lower())
        for email in user_identifiers.get('emails', []):
            if email:
                _self_names.add(email.lower())
                local_part = email.split('@')[0].lower()
                if local_part:
                    _self_names.add(local_part)

        # Load people for person name resolution
        people_db = get_people_by_user(self.user_id, limit=500)

        for action in actions:
            if not isinstance(action, dict):
                continue

            action_text = action.get("action", "")
            if not action_text:
                continue

            action_type = action.get("type", "follow_up")
            person_name = action.get("person")
            deadline = action.get("deadline")

            # HF-04: Skip self-referential actions (exact case-insensitive match only)
            if person_name and isinstance(person_name, str) and _self_names:
                if person_name.strip().lower() in _self_names:
                    logger.warning(
                        "Skipping self-referential action item for item %d: person=%r matches user identity",
                        scanned_item_id, person_name,
                    )
                    continue

            # Try to resolve person name to person_id
            person_id = None
            if person_name and isinstance(person_name, str):
                person_id = self._match_person_by_name(person_name, people_db)

            action_id = insert_detected_action(
                user_id=self.user_id,
                scanned_item_id=scanned_item_id,
                action_text=action_text,
                action_type=action_type,
                person_name=person_name,
                person_id=person_id,
                deadline=deadline,
            )
            if action_id is not None:
                count += 1

        return count
