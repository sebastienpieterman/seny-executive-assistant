# Screen Agent Setup Guide

The Screen Agent is a small program that runs in the background on your Mac or Windows PC. It watches what's on your screen every few minutes and sends you a nudge (via Telegram or Slack) if you've drifted away from whatever you told Seny you'd be working on. Think of it as an ADHD accountability partner that lives in your menu bar.

**You only need to do this setup once.** After that, it starts automatically every time you turn on your computer.

---

## Step 1: Download the Screen Agent Files

1. Open your web browser and go to:\
   **https://github.com/highhands89/seny-executive-assistant**

2. On that page, find the green button that says **Code**. Click it.

3. In the dropdown that appears, click **Download ZIP**.

4. Your browser will download a file. It will be called something like:\
   `seny-executive-assistant-main.zip`\
   It usually lands in your **Downloads** folder.

5. Find the downloaded file and unzip it:
   - **Mac:** Double-click the `.zip` file. macOS will automatically unzip it into a folder right next to the zip file.
   - **Windows:** Right-click the `.zip` file and choose **Extract All**. Click **Extract** in the dialog that appears.

6. Open the unzipped folder. It will be called something like `seny-executive-assistant-main`. Inside, you'll see many folders and files. Look for the one called **`screen_agent`**.

7. Copy the entire `screen_agent` folder somewhere permanent on your computer. Your **Documents** folder is a good choice.

8. When you're done, you should have something like this:
   - **Mac:** `/Users/yourname/Documents/screen_agent/` with files like `agent.py` inside it
   - **Windows:** `C:\Users\yourname\Documents\screen_agent\` with files like `agent.py` inside it

---

## Step 2: Get Your API Key from Seny

1. Open Seny in your web browser (the URL you use to chat with Seny — something like `https://your-app.up.railway.app`).

2. Look at the left sidebar. At the very bottom, there's a **Settings** button (it looks like a gear icon). Click it.

3. A Settings panel will open. Click the **General** tab at the top of the panel.

4. Scroll down until you see a section labeled **Screen Agent**.

5. Click the **Generate Key** button.

6. A long string of letters and numbers will appear. This is your API key. **Copy it** — select the whole thing and press:
   - **Mac:** Cmd + C
   - **Windows:** Ctrl + C

7. Keep this key handy. You'll paste it in the next step.

---

## Step 3: Add Your API Key

You need to create a small configuration file that tells the Screen Agent how to connect to your Seny account.

1. Open the `screen_agent` folder you copied in Step 1 (for example, `Documents/screen_agent`).

2. Look for a file called **`.env.example`**. This is a template file.

   > **Can't see the file?** Files that start with a dot are hidden by default.
   > - **Mac:** In Finder, press **Cmd + Shift + .** (that's the period key). Hidden files will appear slightly faded. Press the same keys again later to hide them.
   > - **Windows:** In File Explorer, click **View** at the top, then check the box for **Hidden items**.

3. Make a copy of `.env.example`:
   - **Mac:** Right-click the file, click **Duplicate**. Rename the copy from `.env.example copy` to `.env` (remove everything after `.env`). If your Mac warns you about changing the file extension, click **Use .env**.
   - **Windows:** Right-click the file, click **Copy**. Then right-click in the same folder and click **Paste**. Rename the pasted file from `.env.example - Copy` to `.env`. If Windows warns you about changing the file extension, click **Yes**.

4. Now open the `.env` file with a plain text editor:
   - **Mac:** Right-click the `.env` file, choose **Open With**, then pick **TextEdit**. If TextEdit isn't listed, choose **Other** and find TextEdit in your Applications folder.
   - **Windows:** Right-click the `.env` file, choose **Open with**, then pick **Notepad**.

5. You'll see a few lines of text. Find the line that says:
   ```
   SCREEN_AGENT_KEY=your_key_here
   ```

6. Delete `your_key_here` and paste the API key you copied in Step 2. The line should now look something like:
   ```
   SCREEN_AGENT_KEY=sa_a1b2c3d4e5f6...
   ```

7. Find the line that says:
   ```
   SENY_URL=http://localhost:8000
   ```

8. Delete `http://localhost:8000` and type your Seny URL instead. This is the same URL you use to open Seny in your browser. For example:
   ```
   SENY_URL=https://your-app.up.railway.app
   ```
   Make sure there is **no trailing slash** at the end (no `/` after `.app`).

9. Save the file and close the text editor:
   - **Mac:** Cmd + S, then Cmd + Q
   - **Windows:** Ctrl + S, then close Notepad

---

## Step 4: Install and Start (Mac)

1. Open Terminal. The easiest way:
   - Press **Cmd + Space** on your keyboard (this opens Spotlight search)
   - Type **Terminal**
   - Press **Enter**

   A window with a dark or white background and a blinking cursor will appear. This is the Terminal.

2. You need to navigate to the folder that *contains* your `screen_agent` folder. If you put `screen_agent` in your Documents folder, the containing folder is Documents. But the install script expects you to be in the folder that contains `screen_agent/agent.py`.

   Type this command exactly as shown and press **Enter**:
   ```
   cd ~/Documents
   ```

3. Now run the install script. Type this command and press **Enter**:
   ```
   bash screen_agent/install_mac.sh
   ```

4. The script will:
   - Install a few small helper programs the agent needs (this may take a minute)
   - Set the agent to start automatically every time you log in
   - Start the agent right now

5. **How to tell it worked:** Look at your Mac's menu bar (the strip along the very top of your screen, where the clock and Wi-Fi icon are). You should see a small eye icon. That means the Screen Agent is running.

   > **Don't see the eye icon?** Wait about 10 seconds — it can take a moment to appear. If it still doesn't show up, see the Troubleshooting section below.

---

## Step 4: Install and Start (Windows)

1. Open **File Explorer** (the folder icon on your taskbar, or press **Windows key + E**).

2. Navigate to the folder that contains your `screen_agent` folder. For example, if you put it in Documents, open `Documents`.

3. Open the `screen_agent` folder.

4. Double-click the file called **`install_windows.bat`**.

5. A black window (Command Prompt) will appear and start running the setup.

6. **If you see a message saying Python is not installed:**
   - The script will show you a download link: **https://www.python.org/downloads**
   - Open that link in your browser
   - Click the big yellow **Download Python** button
   - Run the downloaded installer
   - **IMPORTANT:** On the very first screen of the installer, check the box at the bottom that says **"Add Python to PATH"**. This is critical — don't skip it.
   - Click **Install Now** and wait for it to finish
   - Close the installer
   - Go back to the `screen_agent` folder and double-click **`install_windows.bat`** again

7. If Python is already installed (or after you just installed it), the script will:
   - Install a few small helper programs the agent needs
   - Set the agent to start automatically every time you log in
   - Start the agent right now

8. **How to tell it worked:** Look at the bottom-right corner of your screen, in the **system tray** (the area near the clock). You should see a small Seny icon. You may need to click the small **^** arrow to see it, since Windows sometimes hides tray icons.

9. Press any key to close the Command Prompt window. The agent will keep running in the background.

---

## Pausing, Stopping, and Restarting the Agent

**To pause the agent** (if you need privacy for a bit):
- Click the eye icon (Mac menu bar) or the Seny tray icon (Windows system tray)
- Click **Pause**
- The icon will change to show the agent is paused
- Click **Resume** whenever you want it to start watching again

**To stop the agent completely:**
- Click the tray/menu bar icon
- Click **Quit**
- The agent will stop and won't run again until your next login (or until you restart it manually)

**To restart the agent manually:**
- **Mac:** Open Terminal and run: `bash ~/Documents/screen_agent/install_mac.sh`
- **Windows:** Double-click `install_windows.bat` in your screen_agent folder

---

## Uninstalling the Screen Agent

If you want to remove the Screen Agent completely:

**Mac:**
1. Open Terminal (Cmd + Space, type "Terminal", press Enter)
2. Type this command and press Enter:
   ```
   launchctl unload ~/Library/LaunchAgents/com.seny.screenagent.plist
   ```
3. Then type this command and press Enter:
   ```
   rm ~/Library/LaunchAgents/com.seny.screenagent.plist
   ```
4. Delete the `screen_agent` folder from your Documents (just drag it to the Trash)

**Windows:**
1. Press the **Windows key**, type **Command Prompt**, and press Enter
2. Type this command and press Enter:
   ```
   schtasks /Delete /TN "Seny Screen Agent" /F
   ```
3. Delete the `screen_agent` folder from your Documents

---

## Troubleshooting

### Mac: The eye icon doesn't appear in the menu bar

1. Open Terminal (Cmd + Space, type "Terminal", press Enter)
2. Type this command and press Enter to check the log:
   ```
   cat ~/Library/Logs/SenyScreenAgent/agent.log
   ```
3. Look at the last few lines. Common issues:
   - **"Invalid screen agent key"** — your API key is wrong. Go back to Step 2 and generate a new key, then update your `.env` file (Step 3).
   - **"Connection refused"** or **"Could not connect"** — double-check that your `SENY_URL` in the `.env` file is correct and that Seny is running.

### Windows: The tray icon doesn't appear

1. First, make sure Python is installed correctly. Press the **Windows key**, type **Command Prompt**, press Enter. Then type:
   ```
   python --version
   ```
   If this shows an error instead of a version number (like `Python 3.11.5`), Python isn't installed properly. Go back to Step 4 (Windows) and reinstall Python, making sure to check **"Add Python to PATH"**.

2. If Python is fine, try running the install script again by double-clicking `install_windows.bat`.

### "Invalid screen agent key" errors

Your API key may have changed or expired. To fix this:
1. Open Seny in your browser
2. Go to **Settings** (gear icon, bottom left) and click the **General** tab
3. Scroll to **Screen Agent** and click **Generate Key**
4. Copy the new key
5. Open your `.env` file and replace the old key with the new one
6. Save the file
7. Restart the agent (click the tray icon and choose **Quit**, then start it again)

---

*Last updated: March 2026*
