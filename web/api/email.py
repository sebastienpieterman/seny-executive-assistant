"""
Email/Gmail endpoints for Seny.

OAuth 2.0 flow for Gmail integration with multi-account support:
- GET /api/email/auth-url - Get OAuth authorization URL
- GET /api/email/oauth/callback - Handle OAuth callback
- POST /api/email/oauth/complete - Complete OAuth with code (requires auth)
- GET /api/email/accounts - List connected Gmail accounts
- GET /api/email/status - Check if specific Gmail is connected
- DELETE /api/email/disconnect - Remove Gmail connection

Email UI endpoints:
- GET /api/email/inbox - Get inbox emails for sidebar
- GET /api/email/message/{id} - Get full email content
- POST /api/email/message/{id}/archive - Archive email
- POST /api/email/message/{id}/trash - Move to trash
- POST /api/email/message/{id}/read - Mark as read
- POST /api/email/message/{id}/unread - Mark as unread
"""

import os
import time
import base64
import hashlib
import secrets
from typing import Optional

# Allow OAuth to return more scopes than requested (e.g., if user previously
# granted additional scopes). Without this, oauthlib raises an error when
# returned scopes don't exactly match requested scopes.
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
from fastapi import APIRouter, HTTPException, status, Depends, Request, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from web.auth.jwt_utils import require_auth
from web.core.cache import response_cache
from web.core.database import save_gmail_token, get_gmail_token, delete_gmail_token, list_gmail_tokens
from web.services.gmail_service import GmailService


# Create email router
router = APIRouter()

# Short-lived in-memory store for PKCE code verifiers, keyed by OAuth state.
# Google now requires PKCE for all OAuth flows. The code_verifier is generated
# when the auth URL is created and must be supplied when exchanging the code.
# TTL: 10 minutes (more than enough for a user to complete OAuth).
_pkce_store: dict = {}  # {state: (code_verifier, expires_at)}


def _store_pkce(state: str, code_verifier: str) -> None:
    _pkce_store[state] = (code_verifier, time.time() + 600)
    # Prune expired entries
    now = time.time()
    for k in [k for k, v in _pkce_store.items() if v[1] < now]:
        _pkce_store.pop(k, None)


def _pop_pkce(state: str) -> Optional[str]:
    entry = _pkce_store.pop(state, None)
    if entry and entry[1] > time.time():
        return entry[0]
    return None


def _make_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Google API scopes - Gmail + Calendar + Drive combined OAuth
# Using gmail.modify for read/write email access
# Using calendar (full) for read/write events AND listing all calendars
# Using drive for full read/write access to all Drive files
# Using contacts.readonly for read access to Google Contacts
# Using youtube.readonly for read access to YouTube data
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/youtube.readonly"
]

# OAuth token endpoint
TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_redirect_uri(request: Request) -> str:
    """
    Get the OAuth redirect URI based on the current request.

    Uses request URL to determine if running locally or in production.
    Forces HTTPS in production (reverse proxies report HTTP internally).
    """
    # Get the base URL from the request
    base_url = str(request.base_url).rstrip("/")

    # Force HTTPS for all non-localhost URLs (production, custom domains, etc.)
    is_localhost = "localhost" in base_url or "127.0.0.1" in base_url
    if not is_localhost:
        base_url = base_url.replace("http://", "https://")

    return f"{base_url}/api/email/oauth/callback"


def create_oauth_flow(redirect_uri: str) -> Flow:
    """
    Create a Google OAuth flow for Gmail API.

    Uses client credentials from environment variables.
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gmail OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri]
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=redirect_uri
    )

    return flow


# Response models
class AuthUrlResponse(BaseModel):
    """Response containing OAuth authorization URL."""
    auth_url: str


class GmailAccount(BaseModel):
    """A connected Gmail account."""
    email: str
    created_at: str


class GmailAccountsResponse(BaseModel):
    """Response listing all connected Gmail accounts."""
    accounts: list[GmailAccount]


class GmailStatusResponse(BaseModel):
    """Response for Gmail connection status."""
    connected: bool
    email: Optional[str] = None


class GmailHealthResponse(BaseModel):
    """Response for Gmail health check."""
    connected: bool
    healthy: bool
    email: Optional[str] = None


class DisconnectResponse(BaseModel):
    """Response for disconnect operation."""
    success: bool
    message: str


class OAuthCompleteResponse(BaseModel):
    """Response for OAuth completion."""
    success: bool
    message: str
    email: str


@router.get("/connect")
async def connect_gmail(request: Request, user_id: str = Depends(require_auth)):
    """
    Start Gmail OAuth flow by redirecting to Google.

    Protected endpoint - requires valid JWT token.
    Redirects user directly to Google's authorization page.

    After authorization, Google redirects to /api/email/oauth/callback
    """
    redirect_uri = get_redirect_uri(request)
    flow = create_oauth_flow(redirect_uri)

    code_verifier, code_challenge = _make_pkce_pair()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    _store_pkce(state, code_verifier)

    return RedirectResponse(url=auth_url)


@router.get("/auth-url", response_model=AuthUrlResponse)
async def get_auth_url(request: Request, user_id: str = Depends(require_auth)):
    """
    Get the Google OAuth authorization URL.

    Protected endpoint - requires valid JWT token.
    User should redirect to this URL to begin Gmail authorization.

    Returns:
        Authorization URL to redirect user to
    """
    redirect_uri = get_redirect_uri(request)
    flow = create_oauth_flow(redirect_uri)

    # Generate authorization URL
    # Include access_type=offline to get refresh token
    # Include prompt=consent to always show consent screen (ensures refresh token)
    code_verifier, code_challenge = _make_pkce_pair()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method="S256",
    )
    _store_pkce(state, code_verifier)

    return AuthUrlResponse(auth_url=auth_url)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = None,
    error: str = None,
    state: str = None
):
    """
    Handle OAuth callback from Google.

    This endpoint receives the authorization code from Google after
    the user grants permission. It exchanges the code for tokens
    and stores them in the database.

    Note: This endpoint doesn't use require_auth because the user
    is redirected here from Google. We use a session cookie or
    redirect to a page that handles token storage.

    For now, returns a simple HTML page with status.
    In production, this should redirect to the app with a success/error state.
    """
    if error:
        # User denied access or there was an error
        return HTMLResponse(f"""
            <html>
            <body>
                <h1>Gmail Authorization Failed</h1>
                <p>Error: {error}</p>
                <p><a href="/">Return to Seny</a></p>
            </body>
            </html>
        """)

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No authorization code received"
        )

    # Return a page that completes OAuth using the stored JWT token
    return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Connecting Gmail - Seny</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background: #F9FAFB;
                }}
                .container {{
                    text-align: center;
                    padding: 2rem;
                    background: white;
                    border-radius: 12px;
                    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                    max-width: 400px;
                }}
                h1 {{ color: #1F2937; margin-bottom: 1rem; }}
                p {{ color: #6B7280; margin-bottom: 1rem; }}
                .success {{ color: #22c55e; }}
                .error {{ color: #ef4444; }}
                .spinner {{
                    border: 3px solid #E5E7EB;
                    border-top: 3px solid #4A90E2;
                    border-radius: 50%;
                    width: 40px;
                    height: 40px;
                    animation: spin 1s linear infinite;
                    margin: 1rem auto;
                }}
                @keyframes spin {{
                    0% {{ transform: rotate(0deg); }}
                    100% {{ transform: rotate(360deg); }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Connecting Gmail...</h1>
                <div class="spinner" id="spinner"></div>
                <p id="status">Please wait while we complete the connection.</p>
            </div>
            <script>
                const code = "{code}";
                const state = "{state or ''}";
                const TOKEN_KEY = 'seny_access_token';

                async function completeOAuth() {{
                    const token = localStorage.getItem(TOKEN_KEY);
                    const statusEl = document.getElementById('status');
                    const spinnerEl = document.getElementById('spinner');

                    if (!token) {{
                        statusEl.innerHTML = '<span class="error">Not logged in. Please <a href="/login.html">log in</a> first, then try connecting Gmail again.</span>';
                        spinnerEl.style.display = 'none';
                        return;
                    }}

                    try {{
                        const response = await fetch('/api/email/oauth/complete?code=' + encodeURIComponent(code) + '&state=' + encodeURIComponent(state), {{
                            method: 'POST',
                            headers: {{
                                'Authorization': 'Bearer ' + token,
                                'Content-Type': 'application/json'
                            }}
                        }});

                        const data = await response.json();

                        if (response.ok && data.success) {{
                            statusEl.innerHTML = '<span class="success">✓ Gmail connected successfully!</span><br><br>Redirecting to Seny...';
                            spinnerEl.style.display = 'none';
                            setTimeout(() => {{ window.location.href = '/'; }}, 1500);
                        }} else {{
                            throw new Error(data.detail || 'Failed to complete connection');
                        }}
                    }} catch (error) {{
                        statusEl.innerHTML = '<span class="error">Error: ' + error.message + '</span><br><br><a href="/">Return to Seny</a>';
                        spinnerEl.style.display = 'none';
                    }}
                }}

                completeOAuth();
            </script>
        </body>
        </html>
    """)


@router.post("/oauth/complete", response_model=OAuthCompleteResponse)
async def complete_oauth(
    request: Request,
    code: str,
    state: Optional[str] = None,
    user_id: str = Depends(require_auth)
):
    """
    Complete OAuth flow with authorization code.

    Protected endpoint - requires valid JWT token.
    Called from frontend after user completes OAuth flow.

    Fetches the Gmail address from Google and stores tokens with that email.
    Supports multiple Gmail accounts per user.

    Args:
        code: Authorization code from Google OAuth callback

    Returns:
        Success status with connected email address
    """
    redirect_uri = get_redirect_uri(request)
    flow = create_oauth_flow(redirect_uri)

    # Retrieve the PKCE code_verifier generated when the auth URL was created.
    # Google requires it in the token exchange since enforcing PKCE.
    code_verifier = _pop_pkce(state) if state else None

    try:
        # Exchange authorization code for tokens
        # Note: OAUTHLIB_RELAX_TOKEN_SCOPE env var allows Google to return
        # additional scopes the user previously granted without raising errors
        flow.fetch_token(code=code, code_verifier=code_verifier)
        credentials = flow.credentials

        # Verify we got at least all the scopes we need
        returned_scopes = set(credentials.scopes or [])
        requested_scopes = set(GOOGLE_SCOPES)
        if not requested_scopes.issubset(returned_scopes):
            missing = requested_scopes - returned_scopes
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required permissions: {', '.join(missing)}. "
                       "Please try connecting again and grant all requested permissions."
            )

        # Get the email address from the Gmail profile
        service = build("gmail", "v1", credentials=credentials)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress")

        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not retrieve email address from Gmail"
            )

        # Save tokens to database with email
        save_gmail_token(int(user_id), email, credentials)

        return OAuthCompleteResponse(
            success=True,
            message=f"Gmail account {email} connected successfully",
            email=email
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to complete OAuth: {str(e)}"
        )


@router.get("/accounts", response_model=GmailAccountsResponse)
async def list_gmail_accounts(user_id: str = Depends(require_auth)):
    """
    List all connected Gmail accounts for the user.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of connected Gmail accounts with email and connection date
    """
    cache_key = f"email_accounts_{user_id}"
    cached = response_cache.get(cache_key)
    if cached is not None:
        return cached

    accounts = list_gmail_tokens(int(user_id))
    result = GmailAccountsResponse(
        accounts=[GmailAccount(**acc) for acc in accounts]
    )
    response_cache.set(cache_key, result, ttl_seconds=300)  # 5 min TTL
    return result


@router.get("/status", response_model=GmailStatusResponse)
async def get_gmail_status(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail address to check status for")
):
    """
    Check if a specific Gmail is connected, or if any Gmail is connected.

    Protected endpoint - requires valid JWT token.

    Args:
        email: Optional - specific Gmail to check. If omitted, checks if ANY Gmail is connected.

    Returns:
        Connection status and email (if connected)
    """
    if email:
        # Check specific email
        token_data = get_gmail_token(int(user_id), email)
        if token_data is None:
            return GmailStatusResponse(connected=False)
        return GmailStatusResponse(connected=True, email=email)
    else:
        # Check if any Gmail is connected
        accounts = list_gmail_tokens(int(user_id))
        if not accounts:
            return GmailStatusResponse(connected=False)
        return GmailStatusResponse(connected=True, email=accounts[0]["email"])


@router.get("/health", response_model=GmailHealthResponse)
async def get_gmail_health(user_id: str = Depends(require_auth)):
    """
    Check if Gmail tokens are valid and working.

    Unlike /status which only checks if tokens exist, this endpoint
    actually tests the tokens by attempting a refresh if needed.

    Returns:
        connected: True if tokens exist
        healthy: True if tokens are valid and working
        email: The email address (if connected)
    """
    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        return GmailHealthResponse(connected=False, healthy=False)

    # Test the first account's tokens
    email = accounts[0]["email"]
    gmail = GmailService(int(user_id), email)

    # get_credentials() will attempt refresh and return None if token is revoked
    credentials = await gmail.get_credentials()

    if credentials is None:
        # Token exists but is invalid/revoked
        return GmailHealthResponse(connected=True, healthy=False, email=email)

    return GmailHealthResponse(connected=True, healthy=True, email=email)


@router.delete("/disconnect", response_model=DisconnectResponse)
async def disconnect_gmail(
    user_id: str = Depends(require_auth),
    email: str = Query(..., description="Gmail address to disconnect")
):
    """
    Disconnect a specific Gmail account.

    Protected endpoint - requires valid JWT token.
    Removes stored OAuth tokens for the specified email.

    Args:
        email: Gmail address to disconnect (required)

    Returns:
        Success status
    """
    deleted = delete_gmail_token(int(user_id), email)

    if deleted:
        return DisconnectResponse(
            success=True,
            message=f"Gmail account {email} disconnected successfully"
        )
    else:
        return DisconnectResponse(
            success=False,
            message=f"Gmail account {email} was not connected"
        )


# ============================================================
# Email UI Endpoints
# ============================================================

# Response models for Email UI
class EmailSummary(BaseModel):
    """Email summary for inbox list."""
    id: str
    from_: str  # 'from' is reserved in Python
    subject: str
    snippet: str
    date: str
    is_unread: bool
    account: Optional[str] = None  # Email account this message belongs to (for unified view)
    provider: Optional[str] = None  # "gmail" or "outlook"

    class Config:
        # Allow 'from' in JSON (maps to from_)
        populate_by_name = True

    @classmethod
    def from_gmail(cls, data: dict, account: Optional[str] = None) -> "EmailSummary":
        """Create from Gmail API response."""
        return cls(
            id=data["id"],
            from_=data["from"],
            subject=data["subject"],
            snippet=data["snippet"],
            date=data["date"],
            is_unread=data["is_unread"],
            account=account,
            provider="gmail"
        )


class InboxResponse(BaseModel):
    """Response for inbox endpoint."""
    emails: list[EmailSummary]
    account: str


class EmailDetail(BaseModel):
    """Full email content for preview."""
    id: str
    from_: str
    to: str
    subject: str
    date: str
    body: str
    attachments: list[dict]
    is_unread: bool
    provider: Optional[str] = None  # "gmail" or "outlook"


class ActionResponse(BaseModel):
    """Response for email actions."""
    success: bool
    message: str


def _get_first_account(user_id: int, email: Optional[str]) -> str:
    """Get the email account to use - specified or first connected."""
    if email:
        return email
    accounts = list_gmail_tokens(user_id)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Gmail accounts connected"
        )
    return accounts[0]["email"]


@router.get("/inbox", response_model=InboxResponse)
async def get_inbox(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account to fetch inbox from"),
    max_results: int = Query(10, ge=1, le=50, description="Max emails to return")
):
    """
    Get inbox emails for sidebar display.

    Protected endpoint - requires valid JWT token.

    Args:
        email: Gmail account to use (optional, defaults to first connected)
        max_results: Maximum emails to return (1-50, default 10)

    Returns:
        List of email summaries with unread status
    """
    account_email = _get_first_account(int(user_id), email)

    gmail = GmailService(int(user_id), account_email)
    if not gmail.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Gmail account {account_email} is not connected"
        )

    emails = await gmail.get_inbox(max_results)

    return InboxResponse(
        emails=[EmailSummary.from_gmail(e) for e in emails],
        account=account_email
    )


@router.get("/message/{message_id}", response_model=EmailDetail)
async def get_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account")
):
    """
    Get full email content for preview panel.

    Protected endpoint - requires valid JWT token.

    Args:
        message_id: Gmail message ID
        email: Gmail account to use (optional, defaults to first connected)

    Returns:
        Full email content including body and attachments
    """
    account_email = _get_first_account(int(user_id), email)

    gmail = GmailService(int(user_id), account_email)
    if not gmail.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Gmail account {account_email} is not connected"
        )

    msg = await gmail.read_email(message_id)
    if not msg:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Email not found"
        )

    # Prefer HTML for rich rendering, fallback to plain text wrapped in <pre>
    body_html = msg.get("body_html") or ""
    body_text = msg.get("body_text") or ""
    if body_html:
        body = body_html
    elif body_text:
        body = f"<pre style='white-space:pre-wrap;font-family:inherit'>{body_text}</pre>"
    else:
        body = ""
    # Truncate very long emails
    if len(body) > 50000:
        body = body[:50000] + "\n\n[Email truncated - too long to display]"

    return EmailDetail(
        id=msg["id"],
        from_=msg["from"],
        to=msg["to"],
        subject=msg["subject"],
        date=msg["date"],
        body=body,
        attachments=msg.get("attachments", []),
        is_unread="UNREAD" in msg.get("labelIds", []),
        provider="gmail"
    )


@router.post("/message/{message_id}/archive", response_model=ActionResponse)
async def archive_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account")
):
    """
    Archive an email (remove from inbox).

    Protected endpoint - requires valid JWT token.
    """
    account_email = _get_first_account(int(user_id), email)
    gmail = GmailService(int(user_id), account_email)

    success = await gmail.archive_email(message_id)
    if success:
        response_cache.invalidate(f"email_inbox_{user_id}")
    return ActionResponse(
        success=success,
        message="Email archived" if success else "Failed to archive email"
    )


@router.post("/message/{message_id}/trash", response_model=ActionResponse)
async def trash_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account")
):
    """
    Move an email to trash.

    Protected endpoint - requires valid JWT token.
    """
    account_email = _get_first_account(int(user_id), email)
    gmail = GmailService(int(user_id), account_email)

    success = await gmail.trash_email(message_id)
    if success:
        response_cache.invalidate(f"email_inbox_{user_id}")
    return ActionResponse(
        success=success,
        message="Email moved to trash" if success else "Failed to trash email"
    )


@router.post("/message/{message_id}/read", response_model=ActionResponse)
async def mark_message_read(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account")
):
    """
    Mark an email as read.

    Protected endpoint - requires valid JWT token.
    """
    account_email = _get_first_account(int(user_id), email)
    gmail = GmailService(int(user_id), account_email)

    success = await gmail.mark_read(message_id)
    if success:
        response_cache.invalidate(f"email_inbox_{user_id}")
    return ActionResponse(
        success=success,
        message="Email marked as read" if success else "Failed to mark as read"
    )


@router.post("/message/{message_id}/unread", response_model=ActionResponse)
async def mark_message_unread(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Gmail account")
):
    """
    Mark an email as unread.

    Protected endpoint - requires valid JWT token.
    """
    account_email = _get_first_account(int(user_id), email)
    gmail = GmailService(int(user_id), account_email)

    success = await gmail.mark_unread(message_id)
    if success:
        response_cache.invalidate(f"email_inbox_{user_id}")
    return ActionResponse(
        success=success,
        message="Email marked as unread" if success else "Failed to mark as unread"
    )


@router.get("/inbox/all", response_model=InboxResponse)
async def get_unified_inbox(
    user_id: str = Depends(require_auth),
    max_results: int = Query(10, ge=1, le=50, description="Max emails to return per account")
):
    """
    Get inbox emails from ALL connected Gmail accounts, merged and sorted by date.

    Protected endpoint - requires valid JWT token.

    Args:
        max_results: Maximum emails to fetch per account (1-50, default 10)

    Returns:
        List of email summaries merged from all accounts, sorted by date (newest first)
    """
    import asyncio
    from email.utils import parsedate_to_datetime

    cache_key = f"email_inbox_{user_id}"
    cached = response_cache.get(cache_key)
    if cached is not None:
        return cached

    # Get all connected accounts
    accounts = list_gmail_tokens(int(user_id))

    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Gmail accounts connected"
        )

    account_emails = [acc["email"] for acc in accounts]

    # Fetch emails from all accounts in parallel
    async def fetch_account_inbox(account_email: str) -> list[dict]:
        """Fetch inbox for a single account."""
        try:
            gmail = GmailService(int(user_id), account_email)
            if not gmail.is_connected():
                return []

            emails = await gmail.get_inbox(max_results)

            # Add account email to each message
            for email_data in emails:
                email_data["account"] = account_email

            return emails
        except Exception as e:
            print(f"[Email] Error fetching inbox from {account_email}: {e}")
            return []

    # Fetch all accounts in parallel
    all_inbox_lists = await asyncio.gather(*[
        fetch_account_inbox(email) for email in account_emails
    ])

    # Flatten into single list
    all_emails = []
    for inbox_list in all_inbox_lists:
        all_emails.extend(inbox_list)

    # Sort by date (newest first)
    def parse_date_for_sort(email_data: dict) -> float:
        """Parse email date string to timestamp for sorting."""
        try:
            date_str = email_data.get("date", "")
            # Try parsing as RFC 2822 email date
            dt = parsedate_to_datetime(date_str)
            return dt.timestamp()
        except Exception:
            return 0  # Put unparseable dates at the end

    all_emails.sort(key=parse_date_for_sort, reverse=True)

    # Take top N emails (across all accounts)
    # We fetched max_results per account, now limit total
    all_emails = all_emails[:max_results * 2]  # Show up to 2x for unified view

    result = InboxResponse(
        emails=[EmailSummary.from_gmail(e, e.get("account")) for e in all_emails],
        account="All"  # Indicates unified view
    )
    response_cache.set(cache_key, result, ttl_seconds=180)  # 3 min TTL
    return result
