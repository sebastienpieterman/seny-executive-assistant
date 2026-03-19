# LCD Signal Setup Guide

> **Verified:** Endpoint confirmed working. Test observation posted successfully.

How to make Claude Code automatically tell Seny what you worked on at the end of each session.

---

## Section 1: What This Does

When you finish a Claude Code session in one of your personal projects, Claude will silently send a one-sentence summary to Seny. Seny logs this in its LCD (Live Context Display) observation log, so it knows what you were working on — without you having to narrate anything manually.

This happens automatically, in the background, at the end of each session. It never interrupts your work. If Seny is unreachable, the signal fails silently and nothing breaks.

---

## Section 2: Which Projects to Add This To

| Project | Add the snippet? | Reason |
|---|---|---|
| ✅ Your personal projects | Yes | Owned project — safe to share session context |
| ❌ Client/employer projects | **No** | Client/employer-owned codebase — sending session summaries could violate your IP agreement |

**Rule of thumb:** If you don't own the codebase, don't add the snippet.

---

## Section 3: One-Time Mac Setup — Environment Variables

You only need to do this once. These variables tell the snippet where Seny lives and how to authenticate.

**Step 1:** Open Terminal and run:
```
nano ~/.zshrc
```

**Step 2:** Scroll to the very bottom and add these two lines:
```
export SENY_URL="https://your-seny-instance.example.com"
export SCREEN_AGENT_KEY="your-screen-agent-key-here"
```

Replace `your-screen-agent-key-here` with your actual key. To find it: check your Seny project's `screen_agent/.env` file and look for the line that starts with `SCREEN_AGENT_KEY=`.

**Step 3:** Save and exit nano: press `Ctrl+X`, then `Y`, then `Enter`.

**Step 4:** Apply the changes to your current Terminal window:
```
source ~/.zshrc
```

You're done. These variables will now be available automatically every time you open a new Terminal window.

---

## Section 4: Per-Project Setup — The CLAUDE.md Snippet

Add this block to the `CLAUDE.md` file in each target project. Open the file, scroll to the very bottom, and paste it in.

````markdown
## Seny Session Signal

At the end of each session, send a brief summary to Seny so it knows what was worked on.
Run this bash command silently in the background (do not wait for it):

```bash
python3 -c "
import os, json, urllib.request, subprocess
url = os.environ.get('SENY_URL', '') + '/api/lcd/ingest'
key = os.environ.get('SCREEN_AGENT_KEY', '')
project = os.path.basename(os.getcwd())
if url and key:
    data = json.dumps({'source': 'claude-code', 'project': project, 'content': '[SUMMARY]'}).encode()
    req = urllib.request.Request(url, data=data, headers={'X-Screen-Agent-Key': key, 'Content-Type': 'application/json'})
    try: urllib.request.urlopen(req, timeout=5)
    except: pass
" &
```

Replace `[SUMMARY]` with 1–3 sentences describing what was accomplished this session. Be specific about features, decisions, or problems solved — but do NOT include code snippets, file contents, or proprietary implementation details. High-level only.

Example summaries:
- "Worked on the analytics dashboard — completed the chart builder component. Next session: add export functionality."
- "Side project session — fixed the data pipeline bug causing duplicate entries. Decided to use upsert pattern going forward."
- "Added export-to-CSV feature and fixed sorting bug on the results table."

This runs silently and never interrupts your session. If Seny is unreachable, it fails silently.
````

**Important:** After pasting, replace `[SUMMARY]` with the actual summary text each time Claude runs the command. The `[SUMMARY]` is just a placeholder — Claude will fill it in based on what you did that session.

---

## Section 5: Verification

After setting up the snippet in a project, verify it works end-to-end:

**Step 1:** Start a Claude Code session in one of your target projects.

**Step 2:** Do some work. At the end of the session, ask Claude:
> "Send the session signal to Seny with a summary of what we did."

Claude will run the Python command with a real summary filled in for `[SUMMARY]`.

**Step 3:** Open Seny at `https://your-seny-instance.example.com` and go to the **LCD** page.

**Step 4:** Look at the **Observation Log** — within a few seconds you should see a new entry with:
- `source`: `claude-code`
- `project`: the name of the project folder
- `content`: the summary Claude sent

If the observation appears, the setup is complete and working.

**If it doesn't appear:** Check that you saved the environment variables in Step 3 of Section 3 and ran `source ~/.zshrc`. Also verify the `SCREEN_AGENT_KEY` value matches what's in `screen_agent/.env`.
