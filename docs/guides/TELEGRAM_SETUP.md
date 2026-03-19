# Telegram Integration Setup Guide

This guide walks you through setting up Telegram credentials for Seny. After completing these steps, Seny will be able to read your Telegram messages, search conversations, and send messages.

**Time required:** 10-15 minutes

**Important:** Telegram requires credentials from **two different sources**. You'll need to complete both Part A and Part B below.

---

## What You'll Need

- A Telegram account (the mobile or desktop app installed and logged in)
- A phone number associated with your Telegram account

---

## What Seny Needs from Telegram

| Credential | What It Is | Where You'll Get It |
|------------|-----------|---------------------|
| `TELEGRAM_BOT_TOKEN` | Token for your Seny bot | Part A: @BotFather in Telegram |
| `TELEGRAM_API_ID` | Your Telegram API application ID | Part B: my.telegram.org |
| `TELEGRAM_API_HASH` | Your Telegram API application hash | Part B: my.telegram.org |
| `TELEGRAM_WEBHOOK_SECRET` | A random string to verify webhook requests | You create this yourself |

---

## Part A: Create a Telegram Bot (BotFather)

This creates a bot account that Seny uses to receive messages from you via Telegram.

### Step 1: Open BotFather

1. Open the Telegram app (mobile or desktop)
2. In the search bar at the top, type: **@BotFather**
3. Tap on **"BotFather"** in the search results
   - Look for the one with a blue verified checkmark next to the name
   - The description says "official" — this is Telegram's built-in bot for creating bots
4. Tap **"Start"** at the bottom if this is your first time messaging BotFather

**What you should see:** BotFather sends you a welcome message with a list of commands.

---

### Step 2: Create a New Bot

1. Type (or tap) the command: `/newbot`
2. Press Send

**BotFather asks:** "Alright, a new bot. How are we going to call it? Please choose a name for your bot."

3. Type a display name for your bot, for example: `Seny`
4. Press Send

**BotFather asks:** "Good. Now let's choose a username for your bot. It must end in `bot`. Like this, for example: TetrisBot or tetris_bot."

5. Type a username for your bot, for example: `my_seny_bot`
   - The username must end in `bot` or `_bot`
   - It must be unique across all of Telegram
   - If your first choice is taken, try variations like `seny_assistant_bot` or `youname_seny_bot`
6. Press Send

**What you should see:** BotFather sends a congratulations message that includes a line like:

```
Use this token to access the HTTP API:
1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
```

---

### Step 3: Copy the Bot Token

1. Find the long token in BotFather's message (the part that looks like `1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ`)
2. Copy this entire token — including the numbers, the colon, and the letters after it
3. Save this somewhere safe — this is your `TELEGRAM_BOT_TOKEN`

**Important:** If you lose this token, you can get it again by messaging BotFather with `/token` and selecting your bot. But keep it private — anyone with this token can control your bot.

---

## Part B: Get API Credentials (my.telegram.org)

These credentials let Seny access your personal Telegram messages (not just bot messages). This is what allows Seny to read your conversations with other people.

### Step 4: Go to my.telegram.org

1. Open your web browser
2. Go to: **https://my.telegram.org**

**What you should see:** A page asking for your phone number with the title "Telegram API."

---

### Step 5: Log In

1. Enter your phone number in **international format**
   - For US numbers: `+1` followed by your 10-digit number (e.g., `+12125551234`)
   - For UK numbers: `+44` followed by your number
   - Include the `+` sign and country code
2. Click **"Next"**
3. Telegram sends a **confirmation code to your Telegram app** (not SMS)
   - Open your Telegram app
   - Look for a message from "Telegram" with a login code
4. Enter the code on the website
5. Click **"Sign In"**

**What you should see:** A page with two options: "API development tools" and "Delete account."

---

### Step 6: Create an API Application

1. Click **"API development tools"**

**If you see a form to fill out:**

2. Fill in the form:
   - **App title:** `Seny` (or any name you prefer)
   - **Short name:** `seny` (lowercase, no spaces)
   - **URL:** Leave blank (or enter your Seny URL if you have one)
   - **Platform:** Select **Desktop**
   - **Description:** `Personal AI assistant` (or anything — this is just for your reference)
3. Click **"Create application"**

**If you see your existing application details:**
- You already have an API application. That's fine — you can use the existing credentials.

---

### Step 7: Copy Your API Credentials

After creating the application (or viewing your existing one), you'll see a page with your app details.

Find and copy these two values:

### API ID

1. Find **"App api_id"** (it's a number, like `12345678`)
2. Copy this number

Save this — this is your `TELEGRAM_API_ID`.

### API Hash

1. Find **"App api_hash"** (it's a long string of letters and numbers, like `a1b2c3d4e5f6g7h8i9j0k1l2m3n4`)
2. Copy this string

Save this — this is your `TELEGRAM_API_HASH`.

**Important:** These credentials are permanent and tied to your Telegram account. Keep them private and never share them publicly.

---

## Part C: Set Up Environment Variables

### Step 8: Generate a Webhook Secret

You need a random string for webhook verification. You can create one by:

**Option A — Use a password generator:**
Go to any password generator website and generate a random string of 20+ characters. For example: `xK9mP2nQ7vR4wT6yB3j`

**Option B — Use your terminal (if comfortable):**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save this string — this is your `TELEGRAM_WEBHOOK_SECRET`.

---

### Step 9: Update Environment Variables

#### Local Development (.env file)

Open your `.env` file and add these lines:

```env
TELEGRAM_BOT_TOKEN=your-bot-token-from-step-3
TELEGRAM_API_ID=your-api-id-from-step-7
TELEGRAM_API_HASH=your-api-hash-from-step-7
TELEGRAM_WEBHOOK_SECRET=your-random-string-from-step-8
```

Replace the placeholder values with the actual credentials you copied.

**Example of what it should look like (with fake values):**
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=a1b2c3d4e5f6g7h8i9j0k1l2m3n4
TELEGRAM_WEBHOOK_SECRET=xK9mP2nQ7vR4wT6yB3jL5hF8dC1gA0
```

#### Production (Railway)

1. Go to your Railway dashboard at **https://railway.app/dashboard**
2. Click on your Seny project
3. Go to the **Variables** tab
4. Add these four variables:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_API_ID` = your API ID
   - `TELEGRAM_API_HASH` = your API hash
   - `TELEGRAM_WEBHOOK_SECRET` = your random string

---

## Step 10: Connect Telegram in Seny

1. Restart your Seny server (if running locally) so it picks up the new environment variables
2. Open Seny in your browser
3. Go to **Settings** (gear icon)
4. Click the **Integrations** tab
5. Click **"Connect Telegram"**
6. Follow the on-screen instructions to authorize your Telegram account
   - You'll need to enter the phone number associated with your Telegram account
   - Telegram will send a verification code to your Telegram app
   - Enter the code when prompted

**What you should see:** A success message confirming your Telegram account is connected.

---

## Step 11: Start Your Bot

After connecting, you need to "activate" your bot by sending it a message:

1. Open the Telegram app
2. Search for your bot by its username (the one you created in Step 2, e.g., `@my_seny_bot`)
3. Tap **"Start"** (or send `/start`)

Your bot is now ready to receive messages. You can message your bot in Telegram, and Seny will process and respond.

---

## Step 12: Verify the Connection

After connecting, verify everything is working:

1. In Seny's web chat, try asking: **"What Telegram chats do I have?"**
   - Seny should list some of your Telegram conversations
2. Try: **"Show me recent Telegram messages from [contact name]"**
   - Seny should show recent messages from that person

If both work, your Telegram integration is fully set up.

---

## Troubleshooting

### "Telegram is not connected" error in Seny
- Make sure all four environment variables are set correctly
- Restart your Seny server after adding the variables
- Double-check that the bot token includes both the number part AND the letter part (separated by a colon)

### Can't log in to my.telegram.org
- Make sure you're using the phone number in international format with the `+` prefix
- The confirmation code is sent to your **Telegram app**, not via SMS
- If you don't receive a code, wait a minute and try again
- Some VPNs may block access — try disabling your VPN

### BotFather says the username is taken
- Bot usernames must be unique across all of Telegram
- Try adding your name or a number: `seny_yourname_bot`, `seny_2024_bot`
- The username can be changed later via BotFather's `/setname` command

### "API ID not valid" error
- Make sure you copied the numeric API ID (just the number, no extra characters)
- The API ID should be a number like `12345678`, not the API hash

### "The api_id/api_hash combination is invalid"
- Double-check both `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your environment variables
- Make sure you copied them from my.telegram.org exactly (no extra spaces)
- If you're unsure, log back in to https://my.telegram.org and re-copy the values

### Bot doesn't respond to messages
- Make sure you sent `/start` to your bot in Telegram (Step 11)
- Verify the `TELEGRAM_BOT_TOKEN` is correct
- Check that your Seny server is running

---

## Security Notes

- Your Bot Token, API ID, and API Hash are sensitive — never share them publicly
- The `.env` file is already in `.gitignore` so it won't be committed
- Telegram API credentials are tied to your personal account — treat them like passwords
- You can revoke your bot token anytime by messaging @BotFather with `/revoke`
- You can regenerate API credentials at https://my.telegram.org (but this changes them for all apps using them)
- Tokens and sessions are stored securely in your database

---

*Last updated: March 2026*
