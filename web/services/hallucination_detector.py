"""
Hallucination detection service using Claude Haiku.

Uses a fast, cheap Haiku call to intelligently detect when the main Claude
response claims to have performed an action (create, update, delete, etc.)
without actually calling the corresponding tool.
"""

import json
import logging
import os
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

DETECTION_PROMPT = """You are a hallucination detector for an AI assistant. The assistant has tools it MUST call to perform actions on the user's data (projects, tasks, notes, calendar, email, Slack, Telegram, etc.).

Sometimes the assistant claims to have done something (created, updated, deleted, completed, sent, etc.) WITHOUT actually calling the tool. This is a hallucination.

You will be given:
1. The assistant's response text
2. The list of tools that were actually called during this response

Your job: Determine if the assistant is CLAIMING to have performed a specific action that would require a tool call, but the corresponding tool was NOT in the tools_used list.

Tool mapping (action → required tool):
- Create/add a project → project_create
- Update/change/set a project (status, next action, etc.) → project_update
- Complete/finish a project → project_complete
- Delete/remove a project → project_delete
- List/show projects → project_list or project_get
- Create/add a task → task_create
- Update/change a task → task_update
- Complete/finish/check off a task → task_complete
- Delete/remove a task → task_delete
- List/show tasks → task_list
- Create/write a note → note_create
- Update a note → note_update
- Delete a note → note_delete
- Send an email → email_send
- Create/update/delete calendar event → calendar_create, calendar_update, calendar_delete
- Send a Slack message → slack_send
- Send a Telegram message → telegram_send
- Get idea details → idea_get
- Update/change an idea → idea_update
- Delete/remove an idea → idea_delete
- Add/create a person → people_create
- Update/change a person's info → people_update
- Delete/remove a person → people_delete
- Search projects → project_search
- Reopen a task → task_reopen
- Cancel a task → task_cancel
- Convert/reclassify/turn an item into another type → convert_item

IMPORTANT distinctions:
- If the assistant is ASKING a question ("Would you like me to delete it?"), that is NOT a claim. Return false.
- If the assistant is OFFERING to do something ("I can update that for you"), that is NOT a claim. Return false.
- If the assistant CONFIRMS it did something ("Done! I've updated your project"), that IS a claim. Check if the tool was called.
- If the assistant says it found/searched something, only flag if the corresponding search/list tool wasn't called.

Respond with ONLY valid JSON:
{
  "claimed_action": true/false,
  "action_description": "brief description of what was claimed" or null,
  "expected_tool": "tool_name that should have been called" or null,
  "confidence": 0.0-1.0
}
"""


class HallucinationDetector:
    """Detects when Claude claims actions without calling tools, using Haiku."""

    def __init__(self):
        self.client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = "claude-haiku-4-5-20251001"

    async def check_response(self, response: str, tools_used: list[str]) -> dict:
        """
        Check if the assistant's response claims an action without a tool call.

        Args:
            response: The assistant's response text
            tools_used: List of tool names actually called

        Returns:
            dict with keys:
                - claimed_action: bool
                - action_description: str or None
                - expected_tool: str or None
                - confidence: float
        """
        default_result = {
            "claimed_action": False,
            "action_description": None,
            "expected_tool": None,
            "confidence": 0.0
        }

        try:
            message = f"""{DETECTION_PROMPT}

---

ASSISTANT RESPONSE:
{response}

TOOLS ACTUALLY CALLED:
{json.dumps(tools_used) if tools_used else "[]  (NO tools were called)"}

Analyze and respond with JSON only:"""

            result = await self.client.messages.create(
                model=self.model,
                max_tokens=200,
                messages=[{'role': 'user', 'content': message}]
            )

            response_text = result.content[0].text.strip()

            # Handle markdown code fences
            if response_text.startswith('```'):
                lines = response_text.split('\n')
                response_text = '\n'.join(
                    line for line in lines
                    if not line.startswith('```')
                )

            detection = json.loads(response_text)

            logger.info(
                f"[HALLUCINATION CHECK] claimed={detection.get('claimed_action')}, "
                f"tool={detection.get('expected_tool')}, "
                f"confidence={detection.get('confidence', 0):.2f}, "
                f"tools_used={tools_used}"
            )

            return detection

        except json.JSONDecodeError as e:
            logger.warning(f"Hallucination detector JSON parse error: {e}")
            return default_result
        except Exception as e:
            logger.warning(f"Hallucination detector error: {e}")
            return default_result
