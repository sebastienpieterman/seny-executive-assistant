"""
Entity Resolver - Matches identities across data sources to People DB entries.

Connects email addresses, Slack users, Telegram users, and Google Contacts
to People DB entries via the entity_mappings table.

Phase 13-04 - Scanner Engine & Entity Resolution
"""

import json
import logging
from typing import Optional

from web.core.database import (
    get_db,
    get_people_by_user,
    get_person,
    resolve_entity,
    upsert_entity_mapping,
    update_entity_mapping_person,
)

logger = logging.getLogger(__name__)


class EntityResolver:
    """Discovers identities from scanned data and matches them to People DB entries."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def resolve_all(self) -> dict:
        """
        Run full entity resolution pass.

        1. Harvest identities from scanned_items metadata
        2. Match against People DB
        3. Upsert entity_mappings

        Returns:
            Summary dict: {new_mappings, updated_mappings, unresolved}
        """
        identities = await self.harvest_identities()
        if not identities:
            return {"new_mappings": 0, "updated_mappings": 0, "unresolved": 0}

        matched = await self.match_to_people(identities)

        new_mappings = 0
        updated_mappings = 0
        unresolved = 0

        for m in matched:
            # Check if mapping already exists
            existing = resolve_entity(self.user_id, m["source"], m["identifier"])

            mapping_id = upsert_entity_mapping(
                user_id=self.user_id,
                source=m["source"],
                source_identifier=m["identifier"],
                display_name=m.get("display_name", ""),
                person_id=m.get("person_id"),
                confidence=m.get("confidence", 0.0),
            )

            if mapping_id is not None:
                if existing is None:
                    new_mappings += 1
                else:
                    updated_mappings += 1

            if m.get("person_id") is None:
                unresolved += 1

        logger.info(
            "Entity resolution complete: user=%d new=%d updated=%d unresolved=%d",
            self.user_id, new_mappings, updated_mappings, unresolved,
        )

        return {
            "new_mappings": new_mappings,
            "updated_mappings": updated_mappings,
            "unresolved": unresolved,
        }

    async def harvest_identities(self) -> list[dict]:
        """
        Extract unique identities from scanned_items source_metadata.

        Scans all sources and extracts sender/participant identifiers:
        - Gmail: 'from' email address
        - Slack: user_id + username
        - Telegram: sender_id + sender_name
        - Calendar: attendee emails
        - Contacts: name + emails

        Returns:
            List of identity dicts: [{source, identifier, display_name}, ...]
        """
        seen = set()  # (source, identifier) dedup
        identities = []

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT source, source_metadata
                    FROM scanned_items
                    WHERE user_id = %s AND source_metadata IS NOT NULL
                """, (self.user_id,))

                for row in cursor.fetchall():
                    source = row["source"]
                    try:
                        metadata = json.loads(row["source_metadata"])
                    except (json.JSONDecodeError, TypeError):
                        continue

                    extracted = self._extract_identities_from_metadata(source, metadata)
                    for ident in extracted:
                        key = (ident["source"], ident["identifier"])
                        if key not in seen:
                            seen.add(key)
                            identities.append(ident)

        except Exception as e:
            logger.error("Error harvesting identities for user %d: %s", self.user_id, e)

        logger.info("Harvested %d unique identities for user %d", len(identities), self.user_id)
        return identities

    def _extract_identities_from_metadata(self, source: str, metadata: dict) -> list[dict]:
        """Extract identity entries from a single scanned_item's metadata."""
        results = []

        if source == "gmail":
            from_field = metadata.get("from", "")
            if from_field:
                email, name = self._parse_email_field(from_field)
                if email:
                    results.append({
                        "source": "gmail",
                        "identifier": email.lower(),
                        "display_name": name or email,
                    })

        elif source == "slack":
            user_id = metadata.get("user_id", "")
            username = metadata.get("username", "")
            team_id = metadata.get("team_id", "")
            if user_id:
                # Use team_id:user_id as identifier for uniqueness across workspaces
                identifier = f"{team_id}:{user_id}" if team_id else user_id
                results.append({
                    "source": "slack",
                    "identifier": identifier,
                    "display_name": username or user_id,
                })

        elif source == "telegram":
            sender_id = metadata.get("sender_id")
            sender_name = metadata.get("sender_name", "")
            if sender_id:
                results.append({
                    "source": "telegram",
                    "identifier": str(sender_id),
                    "display_name": sender_name or str(sender_id),
                })

        elif source == "calendar":
            attendees = metadata.get("attendees", [])
            for email in attendees:
                if email and isinstance(email, str):
                    results.append({
                        "source": "calendar",
                        "identifier": email.lower(),
                        "display_name": email,
                    })

        elif source == "contacts":
            name = metadata.get("name", "")
            emails = metadata.get("emails", [])
            for email in emails:
                if email and isinstance(email, str):
                    results.append({
                        "source": "contacts",
                        "identifier": email.lower(),
                        "display_name": name or email,
                    })

        return results

    def _parse_email_field(self, from_field: str) -> tuple[str, str]:
        """
        Parse an email 'From' field like 'Sarah Chen <sarah@co.com>' into (email, name).

        Returns:
            Tuple of (email, display_name). Email may be empty if unparseable.
        """
        from_field = from_field.strip()

        # Format: "Name <email@domain.com>"
        if "<" in from_field and ">" in from_field:
            name_part = from_field[:from_field.index("<")].strip().strip('"').strip("'")
            email_part = from_field[from_field.index("<") + 1:from_field.index(">")].strip()
            return (email_part.lower(), name_part)

        # Format: bare email
        if "@" in from_field:
            return (from_field.lower(), "")

        return ("", "")

    async def match_to_people(self, identities: list[dict]) -> list[dict]:
        """
        Match harvested identities to People DB entries.

        Matching strategies (in priority order):
        1. Existing mapping in entity_mappings table
        2. Email match against People with linked Google Contact emails
        3. Name match (exact, first name, substring)
        4. Unresolved (person_id=None)

        Returns:
            List of identity dicts with person_id and confidence added.
        """
        # Load all people for this user
        people = get_people_by_user(self.user_id, limit=500)

        # Build email-to-person lookup from Google Contacts
        email_to_person = self._build_email_person_map(people)

        results = []
        for ident in identities:
            # Strategy 1: Check existing mapping
            existing = resolve_entity(self.user_id, ident["source"], ident["identifier"])
            if existing and existing.get("person_id"):
                results.append({
                    **ident,
                    "person_id": existing["person_id"],
                    "confidence": existing.get("confidence", 1.0),
                })
                continue

            # Strategy 2: Email match
            person_id = None
            confidence = 0.0

            identifier_lower = ident["identifier"].lower()
            if "@" in identifier_lower and identifier_lower in email_to_person:
                person_id = email_to_person[identifier_lower]
                confidence = 1.0

            # Strategy 3: Name match (only if no email match)
            if person_id is None and ident.get("display_name"):
                person_id, confidence = self._match_by_name(
                    ident["display_name"], people
                )

            results.append({
                **ident,
                "person_id": person_id,
                "confidence": confidence,
            })

        return results

    def _build_email_person_map(self, people: list[dict]) -> dict[str, int]:
        """
        Build a mapping of email addresses to person IDs.

        Uses Google Contacts linked via google_contact_id to find emails.
        """
        email_map = {}

        for person in people:
            google_contact_id = person.get("google_contact_id")
            if not google_contact_id:
                continue

            # Look up the contact's emails from google_contacts table
            try:
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT emails FROM google_contacts
                        WHERE resource_name = %s
                    """, (google_contact_id,))
                    row = cursor.fetchone()
                    if row and row["emails"]:
                        try:
                            emails = json.loads(row["emails"])
                            for email in emails:
                                if isinstance(email, str) and email:
                                    email_map[email.lower()] = person["id"]
                                elif isinstance(email, dict) and email.get("value"):
                                    email_map[email["value"].lower()] = person["id"]
                        except (json.JSONDecodeError, TypeError):
                            pass
            except Exception:
                continue

        return email_map

    def _match_by_name(self, display_name: str, people: list[dict]) -> tuple[Optional[int], float]:
        """
        Match a display name against People DB using simple string comparison.

        Strategies (in order):
        - Exact match (case-insensitive): confidence 1.0
        - First name match: confidence 0.7
        - Substring match (one contains the other): confidence 0.5

        Returns:
            (person_id, confidence) or (None, 0.0) if no match.
        """
        name_normalized = display_name.strip().lower()
        if not name_normalized:
            return (None, 0.0)

        name_first = name_normalized.split()[0] if name_normalized else ""

        best_match = None
        best_confidence = 0.0

        for person in people:
            person_name = (person.get("name") or "").strip().lower()
            if not person_name:
                continue

            # Exact match
            if name_normalized == person_name:
                return (person["id"], 1.0)

            person_first = person_name.split()[0] if person_name else ""

            # First name match (only if first name is at least 2 chars to avoid false positives)
            if name_first and person_first and len(name_first) >= 2 and name_first == person_first:
                if best_confidence < 0.7:
                    best_match = person["id"]
                    best_confidence = 0.7

            # Substring match
            if len(name_normalized) >= 3 and len(person_name) >= 3:
                if name_normalized in person_name or person_name in name_normalized:
                    if best_confidence < 0.5:
                        best_match = person["id"]
                        best_confidence = 0.5

        return (best_match, best_confidence)

    async def link_entity(
        self, source: str, identifier: str, person_id: int, confidence: float = 1.0
    ):
        """Manually link an entity to a person (for user corrections)."""
        upsert_entity_mapping(
            self.user_id, source, identifier,
            display_name=None, person_id=person_id, confidence=confidence,
        )

    async def get_unresolved(self) -> list[dict]:
        """
        Get entity mappings with no person_id linked.

        Returns unique unresolved identities grouped by display_name for
        manual resolution or future AI resolution (Phase 14).
        """
        results = []
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, source, source_identifier, display_name, confidence,
                           created_at, updated_at
                    FROM entity_mappings
                    WHERE user_id = %s AND person_id IS NULL
                    ORDER BY display_name, source
                """, (self.user_id,))
                results = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Error getting unresolved entities for user %d: %s", self.user_id, e)

        return results

    def reconcile_for_new_person(self, person_id: int, person_name: str) -> int:
        """Re-match unresolved entity_mappings against a newly created person.

        Called when a new person is added — checks if any existing unresolved
        mappings now match this person's name.

        Returns count of resolved mappings.
        """
        unresolved = []
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, source, source_identifier, display_name, confidence
                    FROM entity_mappings
                    WHERE user_id = %s AND person_id IS NULL
                    ORDER BY display_name
                """, (self.user_id,))
                unresolved = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("Error fetching unresolved mappings for reconciliation: %s", repr(e))
            return 0

        if not unresolved:
            return 0

        person_record = [{"id": person_id, "name": person_name}]
        count = 0

        for mapping in unresolved:
            display_name = mapping.get("display_name")
            if not display_name:
                continue

            matched_id, confidence = self._match_by_name(display_name, person_record)
            if matched_id and confidence > 0:
                if update_entity_mapping_person(mapping["id"], person_id, confidence):
                    count += 1

        if count > 0:
            logger.info("Reconciled %d unresolved mappings for new person %s (id=%d)",
                        count, person_name, person_id)

        return count
