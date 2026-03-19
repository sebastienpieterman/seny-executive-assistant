# Browser History Sync Setup Guide

The Browser History Sync agent is a small program that runs on your computer and sends your recent Chrome browsing history to Seny. This gives Seny context about what you've been researching, reading, and working on — so when you ask it questions, it already knows what's on your mind.

**What gets synced:** Only page URLs and page titles (like "How to grow tomatoes - Reddit" or "React documentation"). That's it.

**What does NOT get synced:** Page content, passwords, cookies, form data, private/incognito browsing, or anything else. Seny never sees what's *on* the page — just the address and title.

**You only need to do this setup once.** After that, you can set it to sync automatically.

---

## What You Need

- **Google Chrome** installed on your computer (this only works with Chrome)
- **Python** installed on your computer (a free programming tool — instructions below if you don't have it)

### How to check if Python is already installed

**Mac:**
1. Press **Cmd + Space**, type **Terminal**, press **Enter**
2. In the Terminal window, type this and press Enter:
   ```
   python3 --version
   ```
3. If you see something like `Python 3.11.5`, you're good — skip ahead to Step 1.
4. If you see `command not found` or an error, you need to install Python. See below.

**Windows:**
1. Press the **Windows key**, type **Command Prompt**, press **Enter**
2. Type this and press Enter:
   ```
   python --version
   ```
3. If you see something like `Python 3.11.5`, you're good — skip ahead to Step 1.
4. If you see an error, you need to install Python. See below.

### How to install Python (if you don't have it)

1. Go to **https://www.python.org/downloads** in your web browser
2. Click the big yellow **Download Python** button
3. Run the downloaded file to start the installer
4. **IMPORTANT (Windows only):** On the very first screen of the installer, check the box at the bottom that says **"Add Python to PATH"**. This is critical — don't skip it.
5. Click **Install Now** and wait for it to finish
6. Close the installer
7. Close and reopen Terminal (Mac) or Command Prompt (Windows), then try the version check above again to confirm it worked

---

## Step 1: Download the Files

1. Open your web browser and go to:\
   **https://github.com/highhands89/seny-executive-assistant**

2. On that page, find the green button that says **Code**. Click it.

3. In the dropdown that appears, click **Download ZIP**.

4. Your browser will download a file called something like:\
   `seny-executive-assistant-main.zip`\
   It usually lands in your **Downloads** folder.

5. Find the downloaded file and unzip it:
   - **Mac:** Double-click the `.zip` file. macOS will automatically unzip it into a folder right next to the zip file.
   - **Windows:** Right-click the `.zip` file and choose **Extract All**. Click **Extract** in the dialog that appears.

6. Open the unzipped folder (`seny-executive-assistant-main`). Inside, look for the folder called **`agent`**.

7. Copy the entire `agent` folder somewhere permanent on your computer. Your **Documents** folder is a good choice. You could rename it to something like `seny-agent` if you prefer.

8. When you're done, you should have something like:
   - **Mac:** `/Users/yourname/Documents/agent/` with files like `seny_agent.py` inside it
   - **Windows:** `C:\Users\yourname\Documents\agent\` with files like `seny_agent.py` inside it

---

## Step 2: Install the Required Software

The agent needs one small helper program called `requests`. This is a standard tool that lets the agent talk to your Seny server over the internet. Here's how to install it:

**Mac:**

1. Open Terminal (Cmd + Space, type "Terminal", press Enter).

2. Type this command and press Enter:
   ```
   pip3 install requests
   ```

3. You'll see some text scroll by. When it's done and you see your cursor again, it worked. If you see an error about `pip3` not being found, try this instead:
   ```
   python3 -m pip install requests
   ```

**Windows:**

1. Open Command Prompt (press the Windows key, type "Command Prompt", press Enter).

2. Type this command and press Enter:
   ```
   pip install requests
   ```

3. You'll see some text scroll by. When it's done and you see your cursor again, it worked. If you see an error about `pip` not being found, try this instead:
   ```
   python -m pip install requests
   ```

---

## Step 3: Connect to Your Seny Account

Now you'll run the agent's setup wizard, which will ask you for two things: your Seny URL and an API token.

**First, get your API token from Seny:**

1. Open Seny in your web browser.
2. Look at the top-right corner of the screen. Click on your **profile icon** (or the dropdown menu with your name).
3. In the dropdown, click **Desktop Token**.
4. Click the **Generate Token** button.
5. A long string of letters and numbers will appear. Click the **copy button** (the small clipboard icon) next to it to copy the token. Keep this handy for the next part.

   > **Important:** This token gives full access to your Seny account. Don't share it with anyone or post it anywhere public.

**Now run the setup wizard:**

**Mac:**

1. Open Terminal (if it's not already open).

2. Navigate to your agent folder. Type this and press Enter (adjust the path if you put the folder somewhere else):
   ```
   cd ~/Documents/agent
   ```

3. Run the setup wizard:
   ```
   python3 seny_agent.py --setup
   ```

**Windows:**

1. Open Command Prompt (if it's not already open).

2. Navigate to your agent folder. Type this and press Enter (adjust the path if you put the folder somewhere else):
   ```
   cd C:\Users\yourname\Documents\agent
   ```
   Replace `yourname` with your actual Windows username.

3. Run the setup wizard:
   ```
   python seny_agent.py --setup
   ```

**What happens during setup:**

1. It will ask for your **Seny Server URL**. Type your Seny URL (for example, `https://your-app.up.railway.app`) and press Enter. If the default shown is correct, just press Enter.

2. It will check the connection. You should see "Connection successful!"

3. It will ask for your **API Token**. Paste the token you copied above and press Enter:
   - **Mac:** Cmd + V, then Enter
   - **Windows:** Right-click in the Command Prompt window to paste, then press Enter

4. It will verify your token. You should see "Token verified successfully!"

5. It will check if it can find your Chrome history. You should see "Chrome history found."

6. It will ask if you want to do an initial sync right now. Type **y** and press Enter.

7. You should see something like: `Found 150 entries to sync... Sync complete: 150 new entries`

If you see all of that, the setup is done and your browser history is now in Seny.

---

## Step 4: Run a Test Sync

If you skipped the initial sync during setup, or if you just want to test that everything is working, you can run a manual sync at any time.

**Mac:**
```
cd ~/Documents/agent
python3 seny_agent.py --sync
```

**Windows:**
```
cd C:\Users\yourname\Documents\agent
python seny_agent.py --sync
```

**What to expect:**
- If there's new history to sync, you'll see: `Found X entries to sync... Sync complete: X new entries`
- If there's nothing new since the last sync, you'll see: `No new history entries to sync.`
- If Chrome is currently open and locking its history file, you might see: `Could not copy Chrome history`. This is normal — try closing Chrome first, then run the sync again.

---

## Step 5: Set It to Run Automatically (Optional)

Instead of manually running the sync command every time, you can set the agent to run automatically in the background. It will sync your history every 15 minutes.

### Mac: Automatic Startup

This creates a "launch agent" — a Mac feature that runs programs automatically in the background.

1. Open Terminal.

2. First, create the folder where Mac stores these auto-run configurations (it may already exist — that's fine):
   ```
   mkdir -p ~/Library/LaunchAgents
   ```

3. Now you need to find your username. Type this and press Enter:
   ```
   whoami
   ```
   It will print your username (for example, `jane`). Remember this.

4. Open TextEdit to create the configuration file:
   ```
   open -e ~/Library/LaunchAgents/com.seny.agent.plist
   ```
   If TextEdit asks if you want to create a new file, click **Create** or **OK**.

5. Paste the following text into TextEdit. **Before saving**, replace `YOUR_USERNAME` (it appears twice) with the username you found in step 3:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
       <key>Label</key>
       <string>com.seny.agent</string>
       <key>ProgramArguments</key>
       <array>
           <string>/usr/bin/python3</string>
           <string>/Users/YOUR_USERNAME/Documents/agent/seny_agent.py</string>
           <string>--daemon</string>
       </array>
       <key>WorkingDirectory</key>
       <string>/Users/YOUR_USERNAME/Documents/agent</string>
       <key>RunAtLoad</key>
       <true/>
       <key>KeepAlive</key>
       <true/>
       <key>StandardOutPath</key>
       <string>/tmp/seny-agent.log</string>
       <key>StandardErrorPath</key>
       <string>/tmp/seny-agent.err</string>
   </dict>
   </plist>
   ```

6. Save the file (Cmd + S) and close TextEdit.

7. Tell macOS to start using this configuration. In Terminal, run:
   ```
   launchctl load ~/Library/LaunchAgents/com.seny.agent.plist
   ```

8. The agent is now running in the background and will start automatically every time you log in.

**To stop the automatic sync later:**
```
launchctl unload ~/Library/LaunchAgents/com.seny.agent.plist
```

### Windows: Automatic Startup

This uses Windows Task Scheduler — a built-in Windows feature that runs programs automatically.

1. Press the **Windows key**, type **Task Scheduler**, and press **Enter**.

2. On the right side of the Task Scheduler window, click **Create Basic Task**.

3. In the **Name** field, type: `Seny Browser History Sync`

4. Click **Next**.

5. For the trigger, select **When the computer starts**. Click **Next**.

6. For the action, select **Start a program**. Click **Next**.

7. In the **Program/script** field, type: `python`

8. In the **Add arguments** field, type the full path to the agent script with the `--daemon` flag. For example:
   ```
   C:\Users\yourname\Documents\agent\seny_agent.py --daemon
   ```
   Replace `yourname` with your actual Windows username.

9. Click **Next**, review the summary, and click **Finish**.

10. The agent will now start automatically every time your computer starts. It will sync your browser history every 15 minutes in the background.

**To stop the automatic sync later:**
1. Open Task Scheduler
2. Find **Seny Browser History Sync** in the list
3. Right-click it and choose **Delete**

---

## Privacy

- **What's synced:** URLs, page titles, and visit times from Chrome
- **What's NOT synced:** Page content, cookies, passwords, form data, or anything from private/incognito browsing
- **Excluded by default:** localhost, 127.0.0.1, and other private addresses are never synced
- **Custom exclusions:** You can exclude specific websites (like your bank) — see "Excluding Websites" below
- **Encryption:** All data is transmitted securely over HTTPS
- **Your control:** You can delete your synced history from Seny at any time, and you can stop the agent whenever you want

### Excluding Websites

If there are websites you never want synced to Seny (like banking sites or health portals), you can exclude them.

1. Open the configuration file in a text editor. The file is located at:
   - **Mac:** Open Terminal and run: `open -e ~/.seny/config.json`
   - **Windows:** Open File Explorer and navigate to `C:\Users\yourname\.seny\config.json`. Right-click it, choose **Open with**, and pick **Notepad**.

2. Find the `"exclude_domains"` section. Add any websites you want to exclude. For example:
   ```json
   "exclude_domains": [
       "localhost",
       "127.0.0.1",
       "0.0.0.0",
       "mybank.com",
       "health-portal.example.com"
   ]
   ```

3. Save the file. The agent will pick up the changes on the next sync cycle.

---

## Troubleshooting

### "Could not copy Chrome history"

Chrome locks its history file while it's running, which can sometimes prevent the agent from reading it.

**Fix:** Close Chrome completely (make sure it's not still running in the background), then run the sync again. On most systems, the agent works around this automatically, but it can occasionally fail.

### "Invalid or expired API token"

Your token may have expired or been regenerated.

**Fix:**
1. Open Seny in your browser
2. Click your profile icon (top right) and choose **Desktop Token**
3. Click **Generate Token** to create a new one
4. Copy the new token
5. Run the setup wizard again to update it:
   - **Mac:** `cd ~/Documents/agent && python3 seny_agent.py --setup`
   - **Windows:** `cd C:\Users\yourname\Documents\agent` then `python seny_agent.py --setup`

### "Chrome history not found"

The agent looks for Chrome in its default location. This can fail if you:
- Use a non-default Chrome profile
- Use Chromium instead of Chrome
- Installed Chrome to a custom location

**Fix:** This requires editing the agent script, which is more advanced. If you run into this, open an issue at:\
**https://github.com/highhands89/seny-executive-assistant/issues**

### Checking logs

If something isn't working and you want to see what the agent is doing:

- **Mac (if running automatically):** Open Terminal and run:
  ```
  cat /tmp/seny-agent.log
  ```
- **Windows (if running automatically):** Check Task Scheduler — right-click your task and look at the History tab.
- **When running manually:** The agent prints status messages directly in the Terminal/Command Prompt window.

### Checking sync status

You can see your current sync status at any time:

**Mac:**
```
cd ~/Documents/agent
python3 seny_agent.py --status
```

**Windows:**
```
cd C:\Users\yourname\Documents\agent
python seny_agent.py --status
```

This will show you when the last sync happened, how many entries were synced, and whether your connection to Seny is working.

---

## Uninstalling

If you want to completely remove the browser history sync:

1. **Stop the agent** if it's running:
   - If running manually: Press **Ctrl + C** in the Terminal/Command Prompt window
   - If running automatically on Mac: `launchctl unload ~/Library/LaunchAgents/com.seny.agent.plist`
   - If running automatically on Windows: Open Task Scheduler, find "Seny Browser History Sync", right-click, and delete it

2. **Delete the configuration** (this removes your saved token and sync history):
   - **Mac:** Open Terminal and run: `rm -rf ~/.seny`
   - **Windows:** Open File Explorer, navigate to `C:\Users\yourname`, and delete the `.seny` folder (you may need to show hidden files first — click **View** and check **Hidden items**)

3. **Delete the agent files:**
   - Simply delete the `agent` folder from your Documents (drag it to the Trash on Mac, or right-click and Delete on Windows)

4. **Remove the automatic startup** (if you set it up):
   - **Mac:** Open Terminal and run: `rm ~/Library/LaunchAgents/com.seny.agent.plist`
   - **Windows:** If you already deleted the Task Scheduler entry in step 1, you're done

---

*Last updated: March 2026*
