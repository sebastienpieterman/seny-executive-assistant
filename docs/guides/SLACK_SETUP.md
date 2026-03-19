# Slack Integration Setup Guide

This guide walks you through creating a Slack app and connecting it to Seny. After completing these steps, Seny will be able to read and search your Slack messages, list channels, and send messages on your behalf.

**Time required:** 15-20 minutes

---

## What You'll Need

- A Slack workspace where you have **admin access** (or permission to install apps)
- Your Seny instance running (locally or on Railway)

---

## What Seny Needs from Slack

| Credential | What It Is | Where You'll Find It |
|------------|-----------|---------------------|
| `SLACK_CLIENT_ID` | Identifies your Slack app | App settings > Basic Information |
| `SLACK_CLIENT_SECRET` | Secret key for OAuth | App settings > Basic Information |
| `SLACK_SIGNING_SECRET` | Verifies requests from Slack | App settings > Basic Information |

---

## Step 1: Go to the Slack API Dashboard

1. Open your web browser
2. Go to: **https://api.slack.com/apps**
3. Sign in with your Slack account if prompted
   - Use the account that has admin access to the workspace you want to connect

**What you should see:** A page titled "Your Apps" (it may be empty if you haven't created apps before).

---

## Step 2: Create a New App

1. Click the green **"Create New App"** button
2. A dialog appears with two options. Click **"From scratch"**
3. Fill in the form:
   - **App Name:** `Seny` (or any name you prefer)
   - **Pick a workspace to develop your app in:** Select the workspace you want to connect to Seny
4. Click the green **"Create App"** button

**What you should see:** Your app's "Basic Information" page with sections like "Building Apps for Slack" and "App Credentials."

---

## Step 3: Copy Your App Credentials

You're now on the **Basic Information** page. Scroll down to the **"App Credentials"** section.

You need three values from this section:

### Client ID

1. Find **"Client ID"**
2. Click **"Show"** if the value is hidden
3. Copy the value (it looks like a long number, e.g., `1234567890.1234567890123`)

Save this for later — this is your `SLACK_CLIENT_ID`.

### Client Secret

1. Find **"Client Secret"**
2. Click **"Show"**
3. Copy the value (it looks like a random string, e.g., `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6`)

Save this for later — this is your `SLACK_CLIENT_SECRET`.

### Signing Secret

1. Find **"Signing Secret"**
2. Click **"Show"**
3. Copy the value (similar random string)

Save this for later — this is your `SLACK_SIGNING_SECRET`.

**Important:** Keep these values private. Never share them or commit them to version control.

---

## Step 4: Configure OAuth Scopes

Scopes control what Seny can do in your Slack workspace.

### Add User Token Scopes

1. In the left sidebar, click **"OAuth & Permissions"**
2. Scroll down to the **"User Token Scopes"** section
3. Click the **"Add an OAuth Scope"** button
4. Add each of the following scopes one at a time (type the name in the search box and click to select it):

| Scope | What It Allows |
|-------|---------------|
| `channels:history` | Read messages in public channels |
| `channels:read` | List public channels |
| `groups:history` | Read messages in private channels |
| `groups:read` | List private channels |
| `im:history` | Read direct messages |
| `im:read` | List direct message conversations |
| `mpim:history` | Read group direct messages |
| `mpim:read` | List group direct message conversations |
| `search:read` | Search messages and files |
| `chat:write` | Send messages |
| `users:read` | View people in the workspace |

**How to add each scope:**
1. Click **"Add an OAuth Scope"**
2. Start typing the scope name (e.g., `channels:history`)
3. Click on the matching option in the dropdown
4. Repeat for all 11 scopes listed above

### Add Bot Token Scopes

Still on the **"OAuth & Permissions"** page, scroll up to find the **"Bot Token Scopes"** section (it's above User Token Scopes).

1. Click **"Add an OAuth Scope"** in the Bot Token Scopes section
2. Add each of these scopes:

| Scope | What It Allows |
|-------|---------------|
| `im:history` | Read DMs sent to the Seny bot |
| `im:read` | List DM conversations |
| `im:write` | Open DM conversations |
| `chat:write` | Send messages as the Seny bot |
| `users:read` | View people in the workspace |

**What you should see:** Both the "User Token Scopes" and "Bot Token Scopes" sections should list all the scopes you added.

---

## Step 5: Set the Redirect URL

Still on the **"OAuth & Permissions"** page, scroll to the top to find **"Redirect URLs"**.

1. Click **"Add New Redirect URL"**
2. Enter the callback URL for your Seny instance:

**For local development:**
```
http://localhost:8000/api/slack/oauth/callback
```

**For production (Railway or custom domain):**
```
https://your-seny-instance.example.com/api/slack/oauth/callback
```

Replace `your-seny-instance.example.com` with your actual Seny URL.

3. Click **"Add"**
4. Click **"Save URLs"**

**Tip:** You can add both URLs (localhost and production) if you want to use Seny in both environments.

---

## Step 6: Update Your Environment Variables

### Local Development (.env file)

Open your `.env` file and add these lines:

```env
SLACK_CLIENT_ID=your-client-id-here
SLACK_CLIENT_SECRET=your-client-secret-here
SLACK_SIGNING_SECRET=your-signing-secret-here
```

Replace the placeholder values with the credentials you copied in Step 3.

### Production (Railway)

1. Go to your Railway dashboard at **https://railway.app/dashboard**
2. Click on your Seny project
3. Go to the **Variables** tab
4. Add these three variables:
   - `SLACK_CLIENT_ID` = your client ID
   - `SLACK_CLIENT_SECRET` = your client secret
   - `SLACK_SIGNING_SECRET` = your signing secret

---

## Step 7: Install the App to Your Workspace

1. Restart your Seny server (if running locally) so it picks up the new environment variables
2. Open Seny in your browser
3. Go to **Settings** (gear icon)
4. Click the **Integrations** tab
5. Click **"Connect Slack"**
6. You'll be redirected to Slack's authorization page
7. Review the permissions listed
8. Click **"Allow"**

**What you should see:** You'll be redirected back to Seny with a success message confirming your Slack workspace is connected.

---

## Step 8: Verify the Connection

After connecting, verify everything is working:

1. In Seny's chat, try asking: **"What Slack channels do I have?"**
   - Seny should list your Slack channels
2. Try: **"Show me recent messages from #general"** (or any channel name)
   - Seny should show recent messages from that channel

If both work, your Slack integration is fully set up.

---

## Troubleshooting

### "OAuth session expired. Please try again."
- The connection request timed out. Simply go back to Settings > Integrations and click "Connect Slack" again.

### "invalid_client_id" error
- Double-check your `SLACK_CLIENT_ID` in your `.env` file or Railway variables
- Make sure there are no extra spaces or quotes around the value
- Verify the Client ID matches exactly what's shown on your Slack app's Basic Information page

### "redirect_uri_mismatch" error
- The redirect URL in your Slack app settings doesn't match your Seny URL
- Go back to Step 5 and verify the redirect URL is correct
- Make sure `http` vs `https` matches exactly
- Check for trailing slashes

### "missing_scope" error
- You're missing one or more required scopes
- Go back to Step 4 and verify all scopes are added
- You may need to reinstall the app after adding scopes: go to "Install App" in the left sidebar and click "Reinstall to Workspace"

### Seny can't read messages from a channel
- Seny can only read channels it has access to
- For private channels, the Seny app (or your user) needs to be a member of that channel
- In Slack, go to the private channel > Settings > Integrations > Add apps > add Seny

### "Connect Slack" button does nothing
- Make sure `SLACK_CLIENT_ID` is set in your environment variables
- Restart your Seny server after adding the variables
- Check browser console for errors (right-click > Inspect > Console tab)

---

## Security Notes

- Your Client Secret and Signing Secret are sensitive — never share them or commit them to git
- The `.env` file is already in `.gitignore` so it won't be committed
- Tokens are stored encrypted in your database
- You can revoke access anytime by going to **https://api.slack.com/apps**, selecting your app, and clicking "Revoke All Tokens" under OAuth & Permissions
- To completely remove the app, click "Delete App" at the bottom of the Basic Information page

---

*Last updated: March 2026*
