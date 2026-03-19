# Seny Screen Agent — Setup Guide

The Screen Agent is a small program that runs in the background on your Mac or Windows PC. It periodically checks what's on your screen and nudges you via Telegram or Slack if you've drifted away from your priority commitments.

**You only need to do this setup once.**

---

## Step 1: Get Your API Key

1. Open Seny in your browser
2. Go to **Settings** (top right)
3. Click the **General** tab
4. Scroll down to the **Screen Agent** section
5. Click **Generate Key**
6. Copy the key — you'll need it in Step 3

---

## Step 2: Make Sure Python Is Installed (Windows only)

**Skip this step if you're on a Mac.**

On Windows, open File Explorer, navigate to the `screen_agent` folder, and double-click `install_windows.bat`. If Python isn't installed yet, the script will tell you and give you a download link. Install Python from that link — during installation, **check the box that says "Add Python to PATH"** — then double-click the script again.

If Python is already installed, the script will just proceed automatically.

## Step 3: Open a Terminal

**Mac:** Press Command + Space, type "Terminal", press Enter

**Windows:** In File Explorer, navigate to the repo folder, double-click `install_windows.bat`. (You don't need to open Command Prompt manually — just double-click the file.)

---

## Step 4: Add Your API Key

Open the file `screen_agent/.env` in any text editor (Notepad on Windows, TextEdit on Mac).

Change this line:
```
SCREEN_AGENT_KEY=your_key_here
```

To your actual key (paste what you copied in Step 1):
```
SCREEN_AGENT_KEY=abc123your_actual_key_here
```

Save the file.

---

## Step 5: Run the Install Script

**Mac (in Terminal):**
```bash
bash screen_agent/install_mac.sh
```

**Windows (in Command Prompt):**
```
screen_agent\install_windows.bat
```

The script will install the required software and set the agent to auto-start every time you log in.

---

## Verify It's Running

**Mac:** Look for the 👁 eye icon in your menu bar (top-right of your screen). If you see it, the agent is running.

**Windows:** Look for a small green icon in your system tray (bottom-right of your taskbar, near the clock). You may need to click the ^ arrow to see it.

---

## Pausing the Agent

Click the tray icon and select **Pause**. The icon will change to show it's paused. Click **Resume** to start it again.

## Stopping the Agent

Click the tray icon and select **Quit**. The agent will stop until your next login (or until you start it manually).

## Uninstalling

**Mac:** Run in Terminal:
```bash
launchctl unload ~/Library/LaunchAgents/com.seny.screenagent.plist
rm ~/Library/LaunchAgents/com.seny.screenagent.plist
```

**Windows:** Run in Command Prompt:
```
schtasks /Delete /TN "Seny Screen Agent" /F
```

---

## Troubleshooting

**Mac — tray icon not appearing:**
Check the log: `cat ~/Library/Logs/SenyScreenAgent/agent.log`

**Windows — tray icon not appearing:**
Make sure Python is in your PATH. Try running `python --version` in Command Prompt — if it fails, reinstall Python and check "Add to PATH" during setup.

**"Invalid screen agent key" errors in logs:**
Your API key has changed. Go to Seny Settings → General → Screen Agent, generate a new key, and update `screen_agent/.env`.
