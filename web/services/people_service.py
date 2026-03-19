"""
People Service for Second Brain relationship tracking.

Manages the People Database with Google Contacts integration for enrichment.
Users can track relationships, follow-ups, and get relationship insights.

Usage:
    service = PeopleService(user_id)
    person = await service.create_person("Sarah", context="college friend")
    insights = await service.get_relationship_insights()
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from web.core.database import (
    create_person as db_create_person,
    get_person as db_get_person,
    get_people_by_user,
    update_person as db_update_person,
    delete_person as db_delete_person,
    search_people as db_search_people,
    add_person_followup,
    get_person_followups,
    complete_person_followup,
    get_db,
    list_google_tokens,
)

logger = logging.getLogger(__name__)


class PeopleService:
    """
    Service for managing the People Database with Google Contacts integration.

    Provides:
    - Person CRUD with automatic Google Contacts linking
    - Follow-up management for relationship tracking
    - Relationship insights (stale relationships, pending follow-ups)
    """

    def __init__(self, user_id: int):
        """
        Initialize People service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    async def create_person(
        self,
        name: str,
        context: str = None,
        notes: str = None
    ) -> dict:
        """
        Create a new person entry, auto-linking to Google Contact if found.

        Args:
            name: Person's name
            context: Who they are, how you know them
            notes: Freeform notes

        Returns:
            Created person dict with any linked contact info
        """
        # Try to find matching Google Contact
        google_contact_id = None
        contact_info = {}

        try:
            google_accounts = list_google_tokens(self.user_id)
            if google_accounts:
                from web.services.contacts_service import ContactsService
                # Search across all connected Google accounts
                for account in google_accounts:
                    contacts_service = ContactsService(self.user_id, account["email"])
                    contacts = await contacts_service.search_contacts(name, limit=1)
                    if contacts:
                        # Found a match - store the resource_name as google_contact_id
                        google_contact_id = contacts[0].get("resource_name")
                        contact_info = contacts[0]
                        break
        except Exception as e:
            # Google Contacts not available or error - continue without enrichment
            logger.warning(f"Could not link to Google Contacts: {e}")

        # Create person in database
        person_id = db_create_person(
            user_id=self.user_id,
            name=name,
            context=context,
            google_contact_id=google_contact_id,
            notes=notes
        )

        if not person_id:
            raise ValueError("Failed to create person")

        # Return created person with contact enrichment
        person = db_get_person(person_id)
        if contact_info:
            person["google_contact"] = contact_info

        return person

    async def get_person(self, person_id: int) -> Optional[dict]:
        """
        Get person with enriched contact info from Google if linked.

        Args:
            person_id: Person's database ID

        Returns:
            Person dict with Google Contact enrichment, or None if not found
        """
        person = db_get_person(person_id)
        if not person:
            return None

        # Enrich with Google Contact data if linked
        if person.get("google_contact_id"):
            try:
                google_accounts = list_google_tokens(self.user_id)
                if google_accounts:
                    from web.services.contacts_service import ContactsService
                    for account in google_accounts:
                        contacts_service = ContactsService(self.user_id, account["email"])
                        contact = await contacts_service.get_contact(person["google_contact_id"])
                        if contact:
                            person["google_contact"] = contact
                            break
            except Exception as e:
                logger.debug(f"Could not enrich with Google Contact: {e}")

        # Add follow-ups to person
        person["followups"] = get_person_followups(person_id, status="active")

        return person

    async def get_person_by_name(self, name: str) -> Optional[dict]:
        """
        Find person by name (case-insensitive exact or fuzzy match).

        Args:
            name: Person's name to look up

        Returns:
            Person dict or None if not found
        """
        # First try exact match (case-insensitive)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM people
                WHERE user_id = %s AND LOWER(name) = LOWER(%s)
            """, (self.user_id, name))
            row = cursor.fetchone()
            if row:
                return await self.get_person(row["id"])

        # Fall back to FTS search
        results = db_search_people(self.user_id, name, limit=1)
        if results:
            return await self.get_person(results[0]["id"])

        return None

    async def list_people(self, limit: int = 50) -> list:
        """
        List all tracked people for this user.

        Args:
            limit: Maximum number of people to return

        Returns:
            List of person dicts
        """
        people = get_people_by_user(self.user_id, limit=limit)

        # Add pending followup count to each person
        for person in people:
            followups = get_person_followups(person["id"], status="active")
            person["pending_followups"] = len(followups)

        return people

    async def search_people(self, query: str, limit: int = 20) -> list:
        """
        FTS search across people.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching person dicts
        """
        return db_search_people(self.user_id, query, limit=limit)

    async def update_person(self, person_id: int, **fields) -> Optional[dict]:
        """
        Update person fields. Auto-updates updated_at.

        Args:
            person_id: Person's database ID
            **fields: Fields to update (name, context, notes, last_contact_date)

        Returns:
            Updated person dict or None if not found
        """
        success = db_update_person(person_id, **fields)
        if not success:
            return None
        return await self.get_person(person_id)

    async def delete_person(self, person_id: int) -> bool:
        """
        Delete person and their follow-ups.

        Args:
            person_id: Person's database ID

        Returns:
            True if deleted
        """
        return db_delete_person(person_id)

    async def record_contact(self, person_id: int, notes: str = None) -> Optional[dict]:
        """
        Record that you contacted this person today. Updates last_contact_date.

        Args:
            person_id: Person's database ID
            notes: Optional notes about the interaction

        Returns:
            Updated person dict or None if not found
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")

        update_fields = {"last_contact_date": today}
        if notes:
            # Append to existing notes
            person = db_get_person(person_id)
            if person:
                existing_notes = person.get("notes") or ""
                if existing_notes:
                    new_notes = f"{existing_notes}\n\n[{today}] {notes}"
                else:
                    new_notes = f"[{today}] {notes}"
                update_fields["notes"] = new_notes

        success = db_update_person(person_id, **update_fields)
        if not success:
            return None
        return await self.get_person(person_id)

    # =========================================================================
    # Follow-up Management
    # =========================================================================

    async def add_followup(self, person_id: int, content: str) -> Optional[dict]:
        """
        Add a follow-up item for a person.

        Args:
            person_id: Person's database ID
            content: What to remember/follow up on

        Returns:
            Created followup dict or None on error
        """
        followup_id = add_person_followup(person_id, content)
        if not followup_id:
            return None

        return {
            "id": followup_id,
            "person_id": person_id,
            "content": content,
            "status": "active",
            "created_at": datetime.utcnow().isoformat()
        }

    async def get_followups(self, person_id: int, include_completed: bool = False) -> list:
        """
        Get follow-ups for a person.

        Args:
            person_id: Person's database ID
            include_completed: Whether to include completed follow-ups

        Returns:
            List of followup dicts
        """
        status = None if include_completed else "active"
        return get_person_followups(person_id, status=status)

    async def complete_followup(self, followup_id: int) -> bool:
        """
        Mark follow-up as completed.

        Args:
            followup_id: Followup's database ID

        Returns:
            True if updated
        """
        return complete_person_followup(followup_id)

    async def get_pending_followups(self) -> list:
        """
        Get ALL pending follow-ups across all people (for daily digest).

        Returns:
            List of followup dicts with person info
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT f.id, f.person_id, f.content, f.created_at,
                       p.name as person_name
                FROM people_followups f
                JOIN people p ON f.person_id = p.id
                WHERE p.user_id = %s AND f.status = 'active'
                ORDER BY f.created_at ASC
            """, (self.user_id,))

            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Relationship Insights
    # =========================================================================

    async def get_stale_relationships(self, days: int = 30) -> list:
        """
        Get people you haven't contacted in X days.

        Args:
            days: Number of days threshold

        Returns:
            List of person dicts with days_since_contact
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, context, last_contact_date, updated_at
                FROM people
                WHERE user_id = %s
                AND (
                    last_contact_date IS NULL
                    OR last_contact_date < %s
                )
                ORDER BY last_contact_date ASC NULLS FIRST
            """, (self.user_id, cutoff_str))

            results = []
            for row in cursor.fetchall():
                person = dict(row)
                # Calculate days since contact
                if person.get("last_contact_date"):
                    last_date = datetime.strptime(person["last_contact_date"], "%Y-%m-%d")
                    person["days_since_contact"] = (datetime.utcnow() - last_date).days
                else:
                    # Never contacted - use created date
                    person["days_since_contact"] = None  # Unknown
                results.append(person)

            return results

    async def get_recent_contacts(self, days: int = 7) -> list:
        """
        Get people you've contacted recently.

        Args:
            days: Look back this many days

        Returns:
            List of person dicts
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, name, context, last_contact_date
                FROM people
                WHERE user_id = %s
                AND last_contact_date >= %s
                ORDER BY last_contact_date DESC
            """, (self.user_id, cutoff_str))

            return [dict(row) for row in cursor.fetchall()]

    async def get_relationship_insights(self) -> dict:
        """
        Get insights about relationships for daily digest.

        Returns:
            Dict with stale, pending_followups, and recent_contacts
        """
        stale = await self.get_stale_relationships(days=30)
        pending = await self.get_pending_followups()
        recent = await self.get_recent_contacts(days=7)

        return {
            "stale": stale,
            "pending_followups": pending,
            "recent_contacts": recent
        }
