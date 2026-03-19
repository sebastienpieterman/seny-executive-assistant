# Hallucination Guide for AI-Assisted Development

This guide explains LLM hallucination in the context of building AI-powered applications like Seny. It's intended as a **permanent reference** for understanding, preventing, and mitigating hallucinations as features grow more complex.

---

## Table of Contents

1. [Understanding Hallucination](#understanding-hallucination)
2. [Why Tool-Based Systems Hallucinate](#why-tool-based-systems-hallucinate)
3. [The Trust Spectrum](#the-trust-spectrum)
4. [Hallucination Patterns by Feature Type](#hallucination-patterns-by-feature-type)
5. [Design Principles to Minimize Hallucination](#design-principles-to-minimize-hallucination)
6. [Detection Strategies](#detection-strategies)
7. [Mitigation Strategies](#mitigation-strategies)
8. [Testing for Hallucinations](#testing-for-hallucinations)
9. [When Hallucination is Acceptable](#when-hallucination-is-acceptable)
10. [Extending Detection to New Features](#extending-detection-to-new-features)

---

## Understanding Hallucination

### What It Is

In general AI discourse, "hallucination" means the model generates false information presented as fact. In Seny's tool-based architecture, hallucination has a more specific meaning:

> **The model claims to have performed an action or knows a state, without actually using the tools to perform/verify it.**

### The Fundamental Problem

Large Language Models are trained to predict the next most likely token. When you ask "create a task called X", the most fluent response is:

```
"Done! I've created a task called X with ID #5."
```

This response is **grammatically perfect and contextually appropriate** — but it may be completely fabricated. The model can generate this without ever calling `task_create`.

### Why This Matters More Than Factual Hallucination

Traditional hallucination (wrong facts) is annoying but usually obvious. Tool-use hallucination is **dangerous** because:

1. **It looks exactly like success** — user has no reason to doubt
2. **Data integrity is compromised** — user thinks task exists, but it doesn't
3. **Trust erodes slowly** — user only discovers problem later
4. **Debugging is hard** — "it worked yesterday, why not today?"

---

## Why Tool-Based Systems Hallucinate

### 1. Fluency Optimization

LLMs are optimized for fluent, helpful responses. Stopping mid-response to call a tool and wait for results is "less fluent" than just generating a plausible answer.

### 2. Tool Use is Optional

In most architectures (including Seny's), the model CHOOSES whether to call tools. It's not forced to. If the model "feels confident" about the answer, it may skip the tool.

### 3. Context Window Pressure

As conversations get longer, the model may "shortcut" by relying on what it remembers rather than re-fetching data. This is especially problematic for:
- Long conversations
- Multi-step operations
- Sessions that span multiple topics

### 4. Training Data Patterns

The model has seen millions of examples of assistants saying "Done!" after being asked to do something. This pattern is deeply ingrained, even when tool use is required.

### 5. Ambiguous Instructions

If the system prompt isn't crystal clear about WHEN and HOW to use tools, the model will make judgment calls. Those calls often favor fluency over accuracy.

### 6. Overconfidence in Memory

After performing an action, the model "remembers" doing it. If asked about that action later, it may rely on memory instead of re-verifying. This breaks when:
- User made changes through UI
- Another session modified data
- Time has passed and state changed

### 7. Negative Claims Feel "Safe"

When the model says "I couldn't find X" or "that doesn't exist," it feels like a cautious, helpful response. But this is still a hallucination if no tool was called to verify. The model may:
- Assume something doesn't exist based on prior conversation context
- Generate a "not found" response to avoid the latency of a tool call
- Pattern-match on previous "not found" responses without checking

**This is particularly dangerous** because:
- "I don't see that channel" sounds responsible and thorough
- Users trust negative responses ("it doesn't exist") as much as positive ones
- Detection is harder — we're conditioned to look for false positives, not false negatives

---

## The Trust Spectrum

Not all AI outputs need the same level of verification:

```
LOW RISK ←————————————————————————————→ HIGH RISK
(can hallucinate)                        (must verify)

Chitchat    Summaries    Searches    State Claims    Mutations
   ↓            ↓            ↓            ↓              ↓
"Hello!"    "Here's      "I found    "You have      "I deleted
            a summary     3 emails    5 tasks"       task #7"
            of..."       about..."
```

### Low Risk (Acceptable Hallucination)
- Greetings and chitchat
- Explanations and how-to guidance
- Opinion or advice
- Creative content

### Medium Risk (Should Verify)
- Summarizing search results
- Describing fetched data
- Counting items from a list
- Interpreting dates/times

### High Risk (Must Verify)
- Claiming current state ("you have X tasks")
- Reporting action results ("I created task #5")
- Specific IDs, counts, or identifiers
- Anything the user might act on

### Critical Risk (Verify + Confirm)
- Mutations (create, update, delete)
- External actions (send email, create event)
- Irreversible operations
- Actions affecting other people

---

## Hallucination Patterns by Feature Type

### Tasks
| Pattern | Example | Detection |
|---------|---------|-----------|
| Action claim | "I've created task #5" | Check `task_create` in tools_used |
| State claim | "You have no tasks" | Check `task_list` in tools_used |
| Count claim | "You have 12 tasks" | Compare to actual tool result |
| ID fabrication | "Task #47" | Verify ID exists in response |

### Notes
| Pattern | Example | Detection |
|---------|---------|-----------|
| Action claim | "I've saved your note" | Check `note_create` in tools_used |
| Search claim | "I found 3 notes about X" | Check `note_search` in tools_used |
| Content claim | "Your note says..." | Check `note_read` in tools_used |
| Link claim | "This links to Note Y" | Verify link in tool response |

### Email
| Pattern | Example | Detection |
|---------|---------|-----------|
| Send claim | "I've sent the email" | Check `email_send` in tools_used |
| Read claim | "The email says..." | Check `email_read` in tools_used |
| Count claim | "You have 5 unread" | Check `email_search` in tools_used |

### Calendar
| Pattern | Example | Detection |
|---------|---------|-----------|
| Create claim | "I've added the event" | Check `calendar_create` in tools_used |
| State claim | "You're free tomorrow" | Check `calendar_list` in tools_used |
| Delete claim | "Event deleted" | Check `calendar_delete` in tools_used |
| Time claim | "Your meeting is at 3pm" | Verify time in tool response |

### Conversation Memory
| Pattern | Example | Detection |
|---------|---------|-----------|
| Memory claim | "We discussed X last week" | Check `conversation_search` in tools_used |
| Quote claim | "You said '...'" | Verify quote in tool response |

### Slack
| Pattern | Example | Detection |
|---------|---------|-----------|
| Exists claim | "Yes, there is a #channel" | Check `slack_list_channels` in tools_used |
| **Not exists claim** | "I don't see that channel" | Check any Slack tool in tools_used |
| Message fabrication | Shows messages with recent timestamps | Check `slack_read` in tools_used |
| Message content | "The last message says..." | Check `slack_read` in tools_used |
| Search claim | "I found 5 messages about X" | Check `slack_search` in tools_used |

> **Important:** The "not exists" pattern is particularly insidious because it sounds helpful ("I couldn't find that") but is still a hallucination if no tool was called to verify.

---

## Design Principles to Minimize Hallucination

### 1. Make Tools Required, Not Optional

Where possible, design the system so Claude MUST call a tool to answer certain questions.

**Weak:**
```
"You can use task_list to see the user's tasks."
```

**Strong:**
```
"You MUST call task_list to answer any question about the user's tasks.
You CANNOT claim to know their tasks without calling this tool first."
```

### 2. Provide Explicit Examples

Show the model what hallucination looks like and why it's wrong.

```
WRONG (hallucination):
User: "Create a task"
Assistant: "Done! Created task #5" ← NO TOOL WAS CALLED

CORRECT:
User: "Create a task"
Assistant: [calls task_create] → receives ID #5 → "Done! Created task #5"
```

### 3. Use Evidence-Based Self-Check Instructions

Tell the model to reason from evidence — not from the phrases it uses — before describing any action.

**Why phrase-based self-checks fail:**
A phrase-based check ("don't say 'Done!' without calling the tool") only catches specific words. The model can hallucinate by *describing the result* without using any prohibited phrase:

```
WRONG (hallucination — no prohibited phrase used):
User: "Remove the financial motivation line from paragraph 3"
Assistant: "I've removed that line and restructured the paragraph to flow better."
← NO TOOL WAS CALLED. But no prohibited phrase was triggered either.
```

**The evidence-based self-check:**

```
SELF-CHECK RULE — apply before every response involving a mutation:
Ask yourself: "Did I receive a tool result from [note_update / task_update /
calendar_update / etc.] in this response?"
- If YES → you may describe what changed, because you have actual evidence.
- If NO → you have NO evidence the action happened. Do NOT describe it.
          Stop and call the tool NOW.

This applies regardless of phrasing. Describing a changed note, updated
task, or rescheduled event without a tool result in hand is a hallucination
even if you never say "Done!".
```

**Why this works:**
It grounds the check in evidence (did a tool result arrive?) rather than language (did I use a specific phrase?). The model cannot describe a changed state it has no evidence of — and evidence only comes from tool results.

### 4. Make the Source of Truth Explicit

```
The DATABASE is the source of truth, not your memory.
Even if you just did something, verify the current state before claiming it.
```

### 5. Return Rich Tool Results

Tool results should include enough information that the model doesn't need to make anything up.

**Weak tool result:**
```
"Task created successfully."
```

**Strong tool result:**
```
"Task created!

**ID:** 47
**Title:** Buy groceries
**Due:** Tomorrow at 9:00 AM
**Priority:** medium

Use ID 47 to reference this task."
```

### 6. Timestamp Everything

Include timestamps in tool results so the model knows the data is fresh:

```
"Your tasks (as of January 17, 2026 6:45 PM):
- Task #1: Buy milk
- Task #2: Call dentist
```

### 7. Separate Read and Write Operations

Make it clear which tools read data vs. modify data:

```
READ tools (safe to call anytime):
- task_list, note_search, email_read

WRITE tools (require verification):
- task_create, task_delete, email_send
```

### 8. Treat Negative Claims Like Positive Claims

Saying "X doesn't exist" is just as much a claim as "X exists." Both require tool verification.

**Weak:**
```
"NEVER claim a channel exists without calling slack_list_channels."
```

**Strong:**
```
"NEVER claim a channel exists OR doesn't exist without calling slack_list_channels.
NEVER say 'I don't see that channel' without FIRST calling a tool to verify."
```

Include explicit examples of negative hallucinations:

```
WRONG (hallucination):
User: "Show me messages from #old-channel"
Assistant: "I don't see a channel called #old-channel." ← NO TOOL WAS CALLED

CORRECT:
User: "Show me messages from #old-channel"
Assistant: [calls slack_list_channels or slack_read] → "I checked and couldn't find #old-channel in your workspace."
```

---

## Detection Strategies

### Strategy 1: Phrase Matching

Detect when the response contains phrases that imply an action, then verify the tool was called.

```python
action_phrases = ["i've created", "i've deleted", "i've updated", "done!"]
if any(phrase in response.lower() for phrase in action_phrases):
    if expected_tool not in tools_used:
        # HALLUCINATION DETECTED
```

**Pros:** Simple, fast, low false positives
**Cons:** Fundamentally limited — the model can hallucinate by *describing* an action without using any trigger phrase. Example: "I removed that line and restructured the paragraph" triggers no phrase but is a full hallucination if note_update wasn't called. **Do not rely on phrase matching as the primary defense for mutations.**

### Strategy 2: State Claim Detection

Detect when the response claims knowledge about current state.

```python
state_phrases = ["you have", "there are", "your list contains", "no tasks"]
if any(phrase in response.lower() for phrase in state_phrases):
    if query_tool not in tools_used:
        # HALLUCINATION DETECTED
```

**Pros:** Catches "memory reliance" hallucinations
**Cons:** More false positives (model might be quoting tool results)

### Strategy 3: Negative Claim Detection

Detect when the response claims something doesn't exist without verification.

```python
not_found_phrases = [
    "don't see", "couldn't find", "can't find", "not found",
    "doesn't exist", "does not exist", "no channel", "no task",
    "there is no", "there isn't"
]
if any(phrase in response.lower() for phrase in not_found_phrases):
    if not any(tool in tools_used for tool in relevant_tools):
        # HALLUCINATION DETECTED - negative claim without verification
```

**Pros:** Catches the often-overlooked "not found" hallucinations
**Cons:** Need to define "relevant_tools" per context (Slack tools for Slack, task tools for tasks)

**Example implementation (from routes.py):**
```python
channel_not_exists_phrases = [
    "don't see a channel", "couldn't find", "channel not found",
    "channel doesn't exist", "no channel called", "there is no #"
]
slack_tools = {'slack_list_channels', 'slack_read', 'slack_search', 'slack_list_dms'}
claims_channel_not_exists = any(p in response_lower for p in channel_not_exists_phrases)
used_any_slack_tool = bool(set(tools_used) & slack_tools)

if claims_channel_not_exists and not used_any_slack_tool:
    print(f"[HALLUCINATION DETECTED] Negative claim without tool call")
    response += "\n\n⚠️ *I didn't actually check...*"
```

### Strategy 4: ID/Count Validation

Extract specific claims (IDs, counts) and verify against tool results.

```python
# Extract claimed IDs
claimed_ids = re.findall(r'task #?(\d+)', response.lower())

# Check if these IDs came from tool results
for id in claimed_ids:
    if id not in tool_result_ids:
        # POSSIBLE FABRICATION
```

**Pros:** Catches specific fabrications
**Cons:** Complex to implement, requires parsing tool results

### Strategy 5: Behavioral Analysis

Track patterns over time to identify systematic hallucination.

```python
# Log every tool call and response claim
hallucination_log.append({
    'timestamp': now,
    'tools_used': tools_used,
    'claims_made': extract_claims(response),
    'matched': verify_claims(claims, tool_results)
})

# Alert if hallucination rate exceeds threshold
if recent_hallucination_rate() > 0.1:  # 10%
    alert_admin()
```

**Pros:** Catches patterns that single-request detection misses
**Cons:** Requires logging infrastructure

### Strategy 6: Semantic Detection via Secondary LLM (Haiku Detector)

Use a fast, cheap secondary model to read the response and judge whether a claimed action matches a tool call — regardless of phrasing.

```python
# In hallucination_detector.py
detection = await detector.check_response(response, tools_used)
if detection.get("claimed_action") and detection.get("confidence", 0) >= 0.65:
    # Retry with forced tool instruction
```

The detector receives the response text and the `tools_used` list, and returns:
- `claimed_action`: bool — did the response claim an action happened?
- `expected_tool`: what tool should have been called
- `confidence`: 0.0–1.0

**Pros:** Phrase-agnostic — catches narration-style hallucinations that phrase matching misses. Covers all tools in one check.
**Cons:** Adds latency (extra LLM call). The detector's own judgment is a prediction, not a ground-truth check. Threshold tuning required — too high misses borderline cases, too low causes unnecessary retries.

**Current threshold:** 0.65 (catches borderline cases with acceptable false-positive rate)

**Limitation:** The Haiku detector fires *after* the response is generated. It catches and retries hallucinations but does not prevent the first hallucinated response from being composed. The user never sees it if the retry succeeds, but latency increases.

---

## Mitigation Strategies

### Strategy 1: Warning Append

When hallucination is detected, append a warning to the response.

```python
if hallucination_detected:
    response += "\n\n⚠️ I may not have completed this action — please verify."
```

**Pros:** User is informed, can verify
**Cons:** Degraded UX, user must take action

### Strategy 2: Silent Retry

When hallucination is detected, automatically retry with a stronger prompt.

```python
if hallucination_detected:
    retry_response = call_claude_with_stronger_prompt(
        original_request + "\n\nYOU MUST CALL THE TOOL. DO NOT JUST SAY YOU DID IT."
    )
```

**Pros:** Often fixes the issue automatically
**Cons:** Double latency, may cause confusion if both responses show

### Strategy 3: Forced Tool Use

Modify the request to require tool use before any response.

```python
# Inject tool requirement
modified_request = f"""
{original_request}

IMPORTANT: You MUST call the appropriate tool before responding.
Do not respond until you have called the tool and received results.
"""
```

**Pros:** Prevents hallucination proactively
**Cons:** May cause unnecessary tool calls

### Strategy 4: Post-Action Verification (Ralph Loops)

After a mutation, automatically verify the result.

```python
# Claude calls task_create
task_created = await task_service.create_task(...)

# Automatically verify
verification = await task_service.get_task(task_created.id)
if verification is None:
    # Creation actually failed!
    return "Task creation failed — please try again."
```

**Pros:** Catches actual failures, not just hallucinations
**Cons:** Additional latency, more complex

### Strategy 5: User Confirmation Loop

For high-risk actions, require explicit user confirmation.

```
Claude: "I'll delete task #5 'Buy groceries'. Type 'confirm' to proceed."
User: "confirm"
Claude: [actually calls task_delete] "Task #5 deleted."
```

**Pros:** User is in control of mutations
**Cons:** More friction, slower workflow

---

## Architectural Enforcement (Future Improvement)

All instructional defenses (self-check rules, system prompt language) and detection-based defenses (Haiku detector, phrase matching) share a fundamental limitation: **they ask or check, but do not enforce**. The model can skip a self-check. The Haiku detector fires after the fact.

True enforcement means the Python code — not the model — decides whether a response is allowed through.

### What architectural enforcement can solve

| Failure mode | Self-check instruction | Architectural enforcement |
|---|---|---|
| Model ignores its own instructions | ❌ can still happen | ✅ code doesn't ask the model |
| Model's self-knowledge is a prediction | ❌ still a prediction | ✅ code has the real tool log |
| Tool called but returned error | ❌ not covered | ✅ code can read the result |
| Multi-turn memory confusion | ❌ possible | ✅ per-response tracking |
| Read/state claims ("your note says X") | ⚠️ partially | ⚠️ still needs semantic detection |

### What it would look like

For **mutations** (create/update/delete), the `tools_used` list already exists in the code. A rule-based check can enforce without any LLM involvement:

```python
# After response is generated, before it's sent:
if is_mutation_request and required_tool not in tools_used:
    # Force retry — user never sees the hallucinated response
```

The gap: detecting `is_mutation_request` requires understanding the user's intent, which still needs semantic judgment (keywords are too brittle — users say "tweak", "fix", "take out that line", etc.).

### The practical path

The best near-term approach is to move the Haiku detector **upstream** — classify the user's *message* before Seny responds, then use the Anthropic API's `tool_choice` parameter to force a tool call when a mutation is detected:

```python
# If user message is classified as a mutation request:
tool_choice = {"type": "any"}  # Forces Claude to call at least one tool
```

This eliminates the "fires after the fact" problem. The hallucination is prevented rather than caught.

**Status:** Not yet built. Current defense-in-depth (self-check + Haiku detector + retry) is the active approach.

---

## Testing for Hallucinations

### Manual Testing Checklist

When testing a new feature, specifically try to trigger hallucinations:

1. **Ask about state without context**
   - "What tasks do I have?" (in a fresh conversation)
   - Should call `task_list`, not guess

2. **Ask to perform action, then ask about it**
   - "Create a task called Test"
   - "What's the ID of the task you just created?"
   - Should reference the actual tool result

3. **Contradict the model's memory**
   - Delete tasks via UI
   - Ask Claude "delete my tasks"
   - Should call `task_list` first, not assume from memory

4. **Ask for specific counts**
   - "How many tasks do I have?"
   - Verify count matches database

5. **Reference non-existent items**
   - "Show me task #999"
   - Should call tool and report not found, not make up content

6. **Rapid-fire operations**
   - Create, update, delete in quick succession
   - Check each operation actually happened

### Automated Testing Ideas

```python
def test_task_create_hallucination():
    """Verify task_create actually gets called."""
    response, tools_used = chat("Create a task called Test")

    assert 'task_create' in tools_used, "Hallucination: task_create not called"
    assert 'task #' in response.lower(), "Response should mention task ID"

def test_task_list_required():
    """Verify task_list is called when asking about tasks."""
    response, tools_used = chat("What tasks do I have?")

    assert 'task_list' in tools_used, "Hallucination: task_list not called"

def test_no_memory_reliance():
    """Verify model doesn't rely on conversation memory."""
    chat("Create a task called Test")
    # Manually delete task via database
    delete_all_tasks_direct()

    response, tools_used = chat("Delete all my tasks")
    assert 'task_list' in tools_used, "Should check current state"
    assert 'no tasks' in response.lower() or 'empty' in response.lower()
```

---

## When Hallucination is Acceptable

Not every hallucination needs mitigation. Accept hallucination when:

### 1. Low Stakes
- Chitchat and greetings
- Explanations of how things work
- Suggestions and recommendations

### 2. Clearly Speculative
- "I think you might like..."
- "Based on what you said earlier..."
- "It sounds like..."

### 3. User Can Easily Verify
- Information displayed elsewhere in UI
- Data the user just provided
- Public/common knowledge

### 4. Correction is Cheap
- User can simply retry
- No permanent changes made
- No external parties affected

---

## Extending Detection to New Features

When adding a new feature (e.g., "Projects"), follow this process:

### Step 1: Identify Risk Points

List every place the model might hallucinate:

```
Feature: Projects
- Claiming a project exists
- Claiming a project was created
- Claiming project membership
- Claiming project statistics
- Fabricating project IDs
```

### Step 2: Define Detection Phrases

For each risk point, list phrases that would indicate hallucination:

```python
project_action_phrases = [
    "i've created the project", "project created", "i added the project",
    "i've updated the project", "i deleted the project"
]

project_state_phrases = [
    "you have", "projects", "no projects", "your projects include"
]
```

### Step 3: Map to Tools

Identify which tool SHOULD be called for each claim type:

```
"i've created the project" → project_create
"you have X projects" → project_list
"project X contains..." → project_get
```

### Step 4: Implement Detection

Add detection logic in `routes.py`:

```python
# Project hallucination detection
project_tools = {'project_create', 'project_update', 'project_delete'}
project_claim_phrases = ["i've created the project", ...]

claims_project_action = any(p in response_lower for p in project_claim_phrases)
used_project_tool = bool(set(tools_used) & project_tools)

if claims_project_action and not used_project_tool:
    print(f"[HALLUCINATION DETECTED] Project action claimed without tool")
    response += "\n\n⚠️ *Please verify the project was actually created.*"
```

### Step 5: Strengthen System Prompt

Add explicit instructions in `claude_service.py`:

```python
system_prompt += """
**Projects**: You can manage projects with these tools:
- project_create, project_list, project_get, project_update, project_delete

You MUST call project_list before claiming anything about the user's projects.
You MUST call project_create to create a project — you cannot create projects by saying you did.
"""
```

### Step 6: Document

Document the new hallucination patterns for future reference.

### Step 7: Test

Use the manual testing checklist for the new feature.

---

## Summary: The Three Laws of Anti-Hallucination

1. **The model MUST call tools to know state** — never trust memory
2. **The model MUST call tools to change state** — never claim success without tool result
3. **The system MUST verify claims** — detect and warn when rules 1 & 2 are violated

### Defense-in-Depth (Current Implementation)

Three layers, each catching what the previous misses:

```
Layer 1 — Prevention (system prompt self-check rule)
  Seny reasons from evidence before describing any mutation.
  "Did I receive a tool result? If not, I cannot describe the change."
  ↓ catches most hallucinations before they happen
  ↓ fails when: model skips the self-check entirely

Layer 2 — Detection (Haiku detector, threshold 0.65)
  Secondary LLM reads the response and tools_used after generation.
  Phrase-agnostic — catches narration-style hallucinations Layer 1 missed.
  ↓ catches borderline hallucinations
  ↓ fails when: confidence < 0.65, or detector itself errors out

Layer 3 — Correction (retry logic)
  When Layer 2 fires, resend the request with forced tool instruction.
  User never sees the hallucinated response if retry succeeds.
  ↓ fixes most detected hallucinations
  ↓ fails when: retry also hallucinates → warning appended to response
```

### Key insight: evidence over phrases

Phrase-based self-checks are insufficient for mutations. The model can hallucinate by describing a result without using any trigger phrase. **Evidence-based self-checks** ("do I have a tool result?") are phrase-agnostic and more robust. Applied to notes, tasks, Google Calendar, and Outlook Calendar.

---

## Related Documentation

- See the Seny system prompt and `claude_service.py` for the active anti-hallucination instructions.
