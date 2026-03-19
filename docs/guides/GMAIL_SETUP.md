# Gmail Integration Setup Guide

This guide walks you through connecting Seny to your Gmail and Google Calendar. Once connected, you can search, read, and send emails, and manage your calendar events, all through the Seny chat.

**Why is this so complicated?** Google doesn't let apps access your email without verifying who's asking. Since Seny is self-hosted (not a big company's app), you need to create your own "app credentials" in Google's system. This is a one-time setup. It feels like a lot of steps, but you're just clicking through Google's menus and copying a few values. You won't need to write any code.

## Prerequisites

- A Google account with Gmail
- Access to Google Cloud Console (free, uses your existing Google account)

## Step 1: Create GCP Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click the project dropdown (top left) → **New Project**
3. Enter project name: `seny-assistant` (or your preference)
4. Click **Create**
5. Wait for project creation, then select it from the dropdown

## Step 2: Enable Gmail API

1. In your new project, go to **APIs & Services** → **Library**
2. Search for "Gmail API"
3. Click **Gmail API** → **Enable**
4. Wait for the API to be enabled

## Step 3: Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Select **External** user type → **Create**
3. Fill in the required fields:
   - **App name:** `Seny`
   - **User support email:** Your email
   - **Developer contact email:** Your email
4. Click **Save and Continue**
5. On the Scopes page, click **Add or Remove Scopes**
6. Search for and select:
   - `https://www.googleapis.com/auth/gmail.modify` (Read and modify Gmail)
7. Click **Update** → **Save and Continue**
8. On Test Users page, click **Add Users**
9. Add your Gmail address
10. Click **Save and Continue** → **Back to Dashboard**

**Note:** App stays in "Testing" mode during development. Testing mode has 7-day refresh token expiry - you may need to re-authorize periodically.

## Step 4: Create OAuth Client ID

1. Go to **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. Select **Desktop app** as Application type
4. Enter name: `Seny Desktop Client`
5. Click **Create**
6. A dialog shows your Client ID and Client Secret
7. Click **Download JSON** to save credentials file

**Security:** Never commit credentials to version control!

## Step 5: Configure Environment Variables

1. Copy your Client ID and Client Secret from the downloaded JSON
2. Add to your `.env` file:

```bash
# Gmail OAuth
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

3. For Railway deployment, add the same variables in Railway dashboard:
   - Go to your Railway project → **Variables**
   - Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`

## Step 6: Set Redirect URI (For Production)

For deployed applications, you need to add the redirect URI:

1. Go to **APIs & Services** → **Credentials**
2. Click on your OAuth 2.0 Client ID
3. Under **Authorized redirect URIs**, add:
   - `http://localhost:8000/api/email/oauth/callback` (development)
   - `https://your-app.railway.app/api/email/oauth/callback` (production)
4. Click **Save**

## Verification

After setup, verify the integration:

1. Install dependencies: `pip install -r requirements.txt`
2. Start the server: `uvicorn web.main:app --reload`
3. Test the auth URL endpoint:
   ```bash
   curl http://localhost:8000/api/email/auth-url
   ```
   Should return a Google OAuth URL

## Troubleshooting

### "Access blocked: App not verified"
- During testing, only test users added in Step 3.9 can authorize
- Add your email to test users list

### "Invalid redirect URI"
- Ensure redirect URI in GCP Console matches exactly
- Check for trailing slashes, http vs https

### "Refresh token expired"
- Testing mode apps have 7-day refresh token expiry
- Re-authorize by visiting the auth URL again
- For production, submit app for Google verification

### "Scope not authorized"
- Re-check OAuth consent screen scopes
- Ensure `gmail.modify` scope is added

## Scopes Reference

| Scope | Purpose |
|-------|---------|
| `gmail.modify` | Read, compose, send, delete emails (recommended) |
| `gmail.readonly` | Read-only access |
| `gmail.send` | Send-only access |
| `gmail.compose` | Create drafts and send |

We use `gmail.modify` for full email assistant capability.

## Security Notes

- **Never commit** `.env` files or credentials.json
- Store tokens in database, not files
- Use HTTPS in production
- Refresh tokens automatically before expiry
- Generic error messages to users (don't leak OAuth errors)

---

*Last updated: January 2026*
