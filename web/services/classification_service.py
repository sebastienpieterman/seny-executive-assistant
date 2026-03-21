"""
Classification service for Second Brain automatic capture and routing.

Uses Claude Haiku for fast, cheap classification of user messages into:
- people: Relationship info (person with context/follow-up)
- project: Work with next actions
- idea: Insight/thought worth capturing
- none: Just conversation, no capture needed
"""

import json
import logging
from anthropic import AsyncAnthropic
from web.core.database import (
    # People CRUD (using PeopleService for create to get Google Contact linking)
    get_people_by_user, update_person, search_people,
    add_person_followup,
    # Projects CRUD
    create_project,
    # Ideas CRUD
    create_idea,
    # Inbox log
    log_inbox_entry, get_recent_inbox
)
from web.services.people_service import PeopleService
from src.core.config import Config

logger = logging.getLogger(__name__)

# Classification prompt for Claude Haiku
CLASSIFICATION_PROMPT = """Analyze this message and determine if it contains information worth capturing in a Second Brain system.

Classifications:
- **people**: Information about a specific person - who they are, how you know them, things to remember about them, follow-ups to do with them. Examples: "Sarah from work mentioned she's moving to Austin", "Remind me to ask John about the project", "Had coffee with Mike, he's interested in investing"
- **project**: Work/project with status or next actions. Examples: "Need to finish the website redesign - next step is wireframes", "The marketing campaign is blocked waiting on assets", "Started learning Spanish, should practice 20 min daily"
- **idea**: An insight, thought, or idea worth capturing. Examples: "I think we should try a subscription model", "What if we combined X with Y?", "Interesting observation: people prefer..."
- **none**: Just conversation - questions, chitchat, commands to the assistant, or anything that doesn't fit above. Examples: "What's the weather?", "Tell me a joke", "How do I use this feature?", "Thanks!"

For each classification except 'none', extract relevant structured data.

Respond with ONLY valid JSON in this exact format:
{
  "classification": "people|project|idea|none",
  "confidence": 0.0-1.0,
  "extracted": {
    // For people: {"name": "...", "context": "...", "notes": "...", "followup": "..." (if any)}
    // For project: {"name": "...", "next_action": "...", "notes": "..."}
    // For idea: {"title": "...", "summary": "...", "tags": "..."}
    // For none: {}
  },
  "reasoning": "Brief explanation of classification"
}

Confidence scoring:
- 0.8-1.0: Clear, explicit intent ("remind me to ask Sarah about...", "add a task to...")
- 0.5-0.8: Implicit intent ("had coffee with John, he mentioned...", "I should probably...")
- 0.3-0.5: Ambiguous, might be conversation or capture-worthy
- 0.0-0.3: Almost certainly just conversation

CRITICAL RULE — Never extract the message author as a person:
If the message contains "I am [name]", "my name is [name]", "[name] is me", "I'm [name]", or any other first-person self-identification, classify as "none". The person writing the message is never a "people" entry to track — only OTHER people they mention are.
Also classify as "none" if the only name extractable is a generic placeholder like "user", "me", "I", "myself", "you", "someone", "person", "them", or "they".

MESSAGE TO CLASSIFY:
"""


class ClassificationService:
    """Service for classifying and routing user messages to Second Brain databases."""

    def __init__(self, user_id: int):
        """
        Initialize the classification service.

        Args:
            user_id: The user's ID for database operations
        """
        self.user_id = user_id
        self.client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        # Use Haiku for fast, cheap classification
        self.model = "claude-haiku-4-5-20251001"

    async def classify_and_route(self, text: str, skip_routing: bool = False) -> dict:
        """
        Analyze text and route to appropriate database.

        Args:
            text: The user message to classify
            skip_routing: If True, only classify but don't create database entries

        Returns:
            dict: {
                'classification': 'people' | 'project' | 'idea' | 'none',
                'confidence': 0.0-1.0,
                'extracted': { ... extracted fields ... },
                'routed_to_table': str or None,
                'routed_to_id': int or None,
                'action_taken': str description,
                'inbox_log_id': int (ID in inbox_log table)
            }
        """
        result = {
            'classification': 'none',
            'confidence': 0.0,
            'extracted': {},
            'routed_to_table': None,
            'routed_to_id': None,
            'action_taken': 'No capture needed',
            'inbox_log_id': None
        }

        try:
            # Call Claude Haiku for classification
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=500,
                messages=[{
                    'role': 'user',
                    'content': CLASSIFICATION_PROMPT + text
                }]
            )

            # Parse the JSON response
            response_text = response.content[0].text.strip()

            # Handle potential markdown code blocks
            if response_text.startswith('```'):
                # Remove markdown code fences
                lines = response_text.split('\n')
                response_text = '\n'.join(
                    line for line in lines
                    if not line.startswith('```')
                )

            classification_data = json.loads(response_text)

            result['classification'] = classification_data.get('classification', 'none')
            result['confidence'] = classification_data.get('confidence', 0.0)
            result['extracted'] = classification_data.get('extracted', {})

            logger.info(
                f"Classification: {result['classification']} "
                f"(confidence: {result['confidence']:.2f}) "
                f"for text: {text[:50]}..."
            )

            # Route to appropriate database if confidence is sufficient
            if not skip_routing and result['classification'] != 'none':
                await self._route_to_database(result, text)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse classification JSON: {e}")
            result['action_taken'] = f'Classification parse error: {str(e)}'
        except Exception as e:
            logger.error(f"Classification error: {e}")
            result['action_taken'] = f'Classification error: {str(e)}'

        # Always log to inbox (audit trail)
        inbox_id = log_inbox_entry(
            user_id=self.user_id,
            original_text=text,
            classification=result['classification'],
            confidence=result['confidence'],
            routed_to_table=result['routed_to_table'],
            routed_to_id=result['routed_to_id']
        )
        result['inbox_log_id'] = inbox_id

        return result

    async def _route_to_database(self, result: dict, original_text: str) -> None:
        """
        Route classified content to the appropriate database table.

        Args:
            result: The classification result dict (modified in place)
            original_text: The original user message
        """
        classification = result['classification']
        extracted = result['extracted']
        confidence = result['confidence']

        # Only auto-route if confidence is high enough
        if confidence < 0.6:
            result['action_taken'] = f'Low confidence ({confidence:.2f}), not auto-routing'
            return

        try:
            if classification == 'people':
                await self._route_to_people(result, extracted, original_text)
            elif classification == 'project':
                await self._route_to_project(result, extracted)
            elif classification == 'idea':
                await self._route_to_idea(result, extracted)
        except Exception as e:
            logger.error(f"Routing error for {classification}: {e}")
            result['action_taken'] = f'Routing error: {str(e)}'

    # Generic/self-referential names that should never become People entries
    _BLOCKED_NAMES = frozenset([
        'user', 'me', 'i', 'myself', 'you', 'yourself', 'someone', 'person',
        'he', 'she', 'they', 'them', 'their', 'we', 'us', 'anyone', 'everyone',
    ])

    async def _route_to_people(self, result: dict, extracted: dict, original_text: str) -> None:
        """Route to people table, handling existing persons."""
        name = extracted.get('name', '').strip()
        if not name:
            result['action_taken'] = 'No name extracted, skipping people routing'
            return

        # Block generic/self-referential names
        if name.lower() in self._BLOCKED_NAMES:
            result['action_taken'] = f'Blocked generic name "{name}", skipping people routing'
            return

        # Check if person already exists
        existing = search_people(self.user_id, name, limit=5)
        existing_person = None
        for person in existing:
            if person['name'].lower() == name.lower():
                existing_person = person
                break

        if existing_person:
            # Update existing person
            updates = {}
            if extracted.get('context'):
                # Append to existing context
                current_context = existing_person.get('context') or ''
                new_context = extracted.get('context', '')
                if new_context and new_context not in current_context:
                    updates['context'] = f"{current_context}\n{new_context}".strip()
            if extracted.get('notes'):
                current_notes = existing_person.get('notes') or ''
                new_notes = extracted.get('notes', '')
                if new_notes and new_notes not in current_notes:
                    updates['notes'] = f"{current_notes}\n{new_notes}".strip()

            # If person doesn't have Google Contact link, try to link now
            if not existing_person.get('google_contact_id'):
                try:
                    from web.core.database import list_google_tokens
                    from web.services.contacts_service import ContactsService
                    google_accounts = list_google_tokens(self.user_id)
                    for account in google_accounts:
                        contacts_service = ContactsService(self.user_id, account["email"])
                        contacts = await contacts_service.search_contacts(name, limit=1)
                        if contacts:
                            updates['google_contact_id'] = contacts[0].get("resource_name")
                            break
                except Exception as e:
                    logger.warning(f"Error linking to Google Contacts: {e}")

            if updates:
                update_person(existing_person['id'], **updates)

            # Add followup if mentioned
            if extracted.get('followup'):
                add_person_followup(existing_person['id'], extracted['followup'])

            result['routed_to_table'] = 'people'
            result['routed_to_id'] = existing_person['id']
            result['action_taken'] = f"Updated existing person: {name}"
            if extracted.get('followup'):
                result['action_taken'] += f" (added follow-up)"
        else:
            # Create new person using PeopleService for Google Contact auto-linking
            people_service = PeopleService(self.user_id)
            try:
                person = await people_service.create_person(
                    name=name,
                    context=extracted.get('context'),
                    notes=extracted.get('notes')
                )
                person_id = person.get('id') if person else None
            except Exception as e:
                logger.error(f"Error creating person via PeopleService: {e}")
                person_id = None
            if person_id:
                # Add followup if mentioned
                if extracted.get('followup'):
                    add_person_followup(person_id, extracted['followup'])

                result['routed_to_table'] = 'people'
                result['routed_to_id'] = person_id
                result['action_taken'] = f"Created new person: {name}"
                if extracted.get('followup'):
                    result['action_taken'] += f" (with follow-up)"

    async def _route_to_project(self, result: dict, extracted: dict) -> None:
        """Route to projects table.

        NOTE: Unlike other categories, projects are NOT auto-created by capture.
        This prevents duplicates when Claude also calls project_create.
        The capture system logs the detection for visibility, but Claude's
        explicit project tools handle actual creation.
        """
        name = extracted.get('name', '').strip()
        if not name:
            result['action_taken'] = 'No project name extracted, skipping routing'
            return

        # Log what we detected, but don't create - let Claude's project tools handle it
        result['routed_to_table'] = 'projects'
        result['routed_to_id'] = None  # Not created - Claude will create via tool
        result['action_taken'] = f"Detected project mention: {name} (Claude will create via tool)"
        result['extracted'] = extracted  # Pass extracted data for reference

    async def _route_to_idea(self, result: dict, extracted: dict) -> None:
        """Route to ideas table."""
        title = extracted.get('title', '').strip()
        if not title:
            result['action_taken'] = 'No idea title extracted, skipping routing'
            return

        idea_id = create_idea(
            user_id=self.user_id,
            title=title,
            summary=extracted.get('summary'),
            tags=extracted.get('tags')
        )
        if idea_id:
            result['routed_to_table'] = 'ideas'
            result['routed_to_id'] = idea_id
            result['action_taken'] = f"Captured idea: {title}"

    @staticmethod
    def should_skip_classification(text: str) -> bool:
        """
        Check if classification should be skipped for this message.

        Args:
            text: The user message

        Returns:
            True if classification should be skipped
        """
        # User opt-out phrases
        opt_out_phrases = [
            "off the record",
            "don't save this",
            "don't capture this",
            "don't log this",
            "private",
            "confidential"
        ]

        # Commands to Seny about Second Brain itself (shouldn't be captured)
        seny_command_phrases = [
            "delete that capture",
            "delete the capture",
            "delete that from",
            "remove that capture",
            "remove that from my second brain",
            "show me my captures",
            "what did you capture",
            "recent captures",
            "reclassify that",
            "move that to",
            "that should be a",
            "that's not a",
            "from my second brain"
        ]

        text_lower = text.lower()
        if any(phrase in text_lower for phrase in opt_out_phrases):
            return True
        if any(phrase in text_lower for phrase in seny_command_phrases):
            return True

        # Catch inbox/capture number references like "delete number 14", "delete #5", "remove inbox 3"
        import re
        inbox_number_pattern = r'\b(delete|remove)\s*(number|#|inbox|capture)?\s*#?\d+\.?$'
        if re.search(inbox_number_pattern, text_lower):
            return True

        return False
