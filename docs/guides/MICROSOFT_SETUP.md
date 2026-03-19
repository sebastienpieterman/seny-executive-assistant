# Microsoft Integration Setup Guide

This guide walks you through connecting Seny to your Microsoft Outlook email and calendar. Once connected, you can search, read, and send emails, and manage your Outlook calendar events, all through the Seny chat.

**Why do I need to do this?** Microsoft requires every app that accesses your email to be registered in their system. Since Seny runs on your own server, you need to create your own registration. This is a one-time setup and you won't be charged anything.

---

## What You'll Need

- A Microsoft account (either personal Outlook.com/Hotmail OR work Microsoft 365)
- Access to the Azure Portal (free - uses your existing Microsoft account)

---

## Step 1: Go to Azure Portal

1. Open your web browser
2. Go to: **https://portal.azure.com**
3. Sign in with your Microsoft account
   - Use the same account you want Seny to access
   - If you have both personal and work accounts, you can use either - you'll connect specific accounts later

**First time?** Azure Portal is free. It's where Microsoft manages developer settings. You won't be charged anything.

---

## Step 2: Find App Registrations

1. In the search bar at the top of the Azure Portal, type: **App registrations**
2. Click on **"App registrations"** in the search results (it has a grid icon)

![Search for App registrations](You'll see a search bar at the very top of the page)

---

## Step 3: Create New Registration

1. Click the **"+ New registration"** button (top left area)

You'll see a form with several fields.

---

## Step 4: Fill Out the Registration Form

Fill in these fields exactly:

### Name
```
Seny
```
(Or any name you prefer - this is just for your reference)

### Supported account types

Select this option:
```
Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant) and personal Microsoft accounts (e.g. Skype, Xbox)
```

**Why this option?** It allows both personal (Outlook.com) AND work (Microsoft 365) accounts.

### Redirect URI

1. In the first dropdown, select: **Web**
2. In the text box, enter your callback URL:

**For local development:**
```
http://localhost:8000/api/microsoft/oauth/callback
```

**For production (Railway):**
```
https://your-seny-instance.example.com/api/microsoft/oauth/callback
```

**Important:** You'll add BOTH URLs, but start with one. We'll add the second one later.

---

## Step 5: Click Register

Click the blue **"Register"** button at the bottom.

You'll be taken to your app's overview page.

---

## Step 6: Copy Your Client ID

On the app overview page, you'll see several IDs. Find and copy:

**Application (client) ID**

It looks like this: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`

**Save this somewhere!** You'll need it for your `.env` file:
```
MICROSOFT_CLIENT_ID=paste_your_client_id_here
```

---

## Step 7: Create a Client Secret

1. In the left sidebar, click **"Certificates & secrets"**
2. Click the **"+ New client secret"** button
3. Fill in:
   - **Description:** `Seny OAuth`
   - **Expires:** Select `24 months` (or your preference)
4. Click **"Add"**

**IMPORTANT: Copy the secret VALUE immediately!**

After you navigate away, you will NEVER be able to see this value again.

The secret looks like this: `abc123~DEFghiJKL456mnoPQR789stuVWX`

**Copy the VALUE column, NOT the Secret ID column!**

Save this for your `.env` file:
```
MICROSOFT_CLIENT_SECRET=paste_your_secret_value_here
```

---

## Step 8: Add API Permissions

1. In the left sidebar, click **"API permissions"**
2. Click **"+ Add a permission"**
3. Click **"Microsoft Graph"** (the first option, with the blue icon)
4. Click **"Delegated permissions"**

Now add these 5 permissions (use the search box to find each one):

| Permission | What it does |
|------------|--------------|
| `Mail.Read` | Read your emails |
| `Mail.Send` | Send emails on your behalf |
| `Calendars.ReadWrite` | Read and create calendar events |
| `User.Read` | Get your email address and name |
| `offline_access` | Stay connected without re-authenticating |

**How to add each one:**
1. Type the permission name in the search box (e.g., `Mail.Read`)
2. Check the checkbox next to it
3. Repeat for all 5 permissions
4. Click the **"Add permissions"** button at the bottom

---

## Step 9: Verify Permissions

After adding, your permissions list should show:

| Permission | Type | Status |
|------------|------|--------|
| Calendars.ReadWrite | Delegated | Granted for... (or "Not granted") |
| Mail.Read | Delegated | Granted for... (or "Not granted") |
| Mail.Send | Delegated | Granted for... (or "Not granted") |
| offline_access | Delegated | Granted for... (or "Not granted") |
| User.Read | Delegated | Granted for... (or "Not granted") |

**"Not granted" is OK for personal accounts!** The permissions will be granted when you connect your account in Seny.

**For work accounts:** If you see a "Grant admin consent" button and you're an admin, click it. If not, your IT admin may need to approve.

---

## Step 10: Add Second Redirect URI (Optional but Recommended)

If you want both local development AND production to work:

1. In the left sidebar, click **"Authentication"**
2. Under "Web" → "Redirect URIs", click **"Add URI"**
3. Add the other URL:
   - If you added localhost first, now add: `https://your-seny-instance.example.com/api/microsoft/oauth/callback`
   - If you added production first, now add: `http://localhost:8000/api/microsoft/oauth/callback`
4. Click **"Save"** at the top

---

## Step 11: Update Your Environment Variables

### Local Development (.env file)

Open your `.env` file and add these lines:

```env
MICROSOFT_CLIENT_ID=your_application_client_id_here
MICROSOFT_CLIENT_SECRET=your_client_secret_value_here
```

Replace the placeholder values with what you copied in Steps 6 and 7.

### Production (Railway)

1. Go to your Railway dashboard
2. Click on your Seny project
3. Go to the **Variables** tab
4. Add these two variables:
   - `MICROSOFT_CLIENT_ID` = your application client ID
   - `MICROSOFT_CLIENT_SECRET` = your client secret value

---

## Step 12: Verify Setup

Your Azure App Registration is complete when you have:

- [ ] Application (client) ID copied
- [ ] Client secret VALUE copied
- [ ] All 5 API permissions added
- [ ] Both redirect URIs added (localhost + production)
- [ ] Environment variables set in `.env`
- [ ] Environment variables set in Railway

---

## Troubleshooting

### "The redirect URI is not valid"
- Make sure the URL exactly matches what you registered
- Check for trailing slashes - they must match exactly
- Verify you selected "Web" (not SPA or other options)

### "Need admin approval" (work accounts)
- Your organization requires IT admin to approve third-party apps
- Contact your IT department with the app name and permissions needed
- Personal accounts don't have this restriction

### "Invalid client secret"
- You may have copied the Secret ID instead of the Secret VALUE
- Secret values look like: `abc123~DEFghiJKL...`
- Secret IDs look like GUIDs: `a1b2c3d4-e5f6-7890-...`
- If unsure, create a new secret and copy the VALUE immediately

### "Token expired"
- Client secrets expire after the period you selected (max 24 months)
- When it expires, create a new secret and update your environment variables
- Consider setting a calendar reminder before expiration

---

## What's Next?

After completing this setup:

1. Restart your local Seny server (if running)
2. Go to Seny Settings
3. Click "Connect Microsoft"
4. Sign in with your Microsoft account
5. Grant the permissions when prompted

You'll then be able to ask Seny about your Outlook emails and calendar!

---

## Security Notes

- Your client secret is sensitive - never share it or commit it to git
- The `.env` file is already in `.gitignore`
- Tokens are stored encrypted in your local SQLite database
- You can revoke access anytime from https://account.microsoft.com/privacy/app-access

---

*Last updated: January 2026*
