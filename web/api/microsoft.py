"""
Microsoft OAuth and API endpoints for Seny.

OAuth 2.0 flow for Microsoft Graph API integration (Outlook + Calendar):
- GET /api/microsoft/auth-url - Get OAuth authorization URL
- GET /api/microsoft/oauth/callback - Handle OAuth callback
- POST /api/microsoft/oauth/complete - Complete OAuth with code (requires auth)
- GET /api/microsoft/accounts - List connected Microsoft accounts
- GET /api/microsoft/status - Check if Microsoft is connected
- DELETE /api/microsoft/disconnect - Remove Microsoft connection

Email endpoints:
- GET /api/microsoft/inbox - Get inbox emails
- GET /api/microsoft/message/{id} - Get full email content
- POST /api/microsoft/message/{id}/archive - Archive email
- POST /api/microsoft/message/{id}/trash - Move to trash
- POST /api/microsoft/message/{id}/read - Mark as read
- POST /api/microsoft/message/{id}/unread - Mark as unread

Calendar endpoints:
- GET /api/microsoft/calendars - List calendars
- GET /api/microsoft/events - Get calendar events
- GET /api/microsoft/event/{id} - Get single event
- POST /api/microsoft/event - Create event
- PUT /api/microsoft/event/{id} - Update event
- DELETE /api/microsoft/event/{id} - Delete event
"""

import asyncio
import os
import secrets
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, status, Depends, Request, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import (
    save_microsoft_token,
    get_microsoft_token,
    delete_microsoft_token,
    list_microsoft_tokens
)
from web.services.outlook_service import OutlookService
from web.services.outlook_calendar_service import OutlookCalendarService


# Create router
router = APIRouter()


# OAuth Configuration
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")

# Microsoft OAuth endpoints (using /common/ for both personal and work accounts)
AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

# Required scopes for email and calendar access
MICROSOFT_SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
    "User.Read",
    "offline_access"
]


def get_redirect_uri(request: Request) -> str:
    """
    Get the OAuth redirect URI based on the current request.

    Forces HTTPS in production (reverse proxies report HTTP internally).
    """
    base_url = str(request.base_url).rstrip("/")

    # Force HTTPS for all non-localhost URLs (production, custom domains, etc.)
    is_localhost = "localhost" in base_url or "127.0.0.1" in base_url
    if not is_localhost:
        base_url = base_url.replace("http://", "https://")

    return f"{base_url}/api/microsoft/oauth/callback"


# Response models
class AuthUrlResponse(BaseModel):
    auth_url: str


class MicrosoftAccount(BaseModel):
    email: str
    account_type: str
    created_at: str

    @classmethod
    def __get_validators__(cls):
        yield cls

    model_config = {"arbitrary_types_allowed": True}

    @staticmethod
    def _coerce(v):
        return str(v) if v is not None else ""

    def __init__(self, **data):
        if "created_at" in data and data["created_at"] is not None:
            data["created_at"] = str(data["created_at"])
        super().__init__(**data)


class MicrosoftAccountsResponse(BaseModel):
    accounts: list[MicrosoftAccount]


class MicrosoftStatusResponse(BaseModel):
    connected: bool
    email: Optional[str] = None
    account_type: Optional[str] = None


class DisconnectResponse(BaseModel):
    success: bool
    message: str


class OAuthCompleteResponse(BaseModel):
    success: bool
    message: str
    email: str


# ============================================================================
# OAuth Endpoints
# ============================================================================

@router.get("/auth-url", response_model=AuthUrlResponse)
async def get_auth_url(request: Request, user_id: str = Depends(require_auth)):
    """
    Get the Microsoft OAuth authorization URL.

    Protected endpoint - requires valid JWT token.
    User should redirect to this URL to begin Microsoft authorization.
    """
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Microsoft OAuth not configured. Set MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET."
        )

    redirect_uri = get_redirect_uri(request)
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    params = {
        "client_id": MICROSOFT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(MICROSOFT_SCOPES),
        "response_mode": "query",
        "state": state,
        "prompt": "consent"  # Always show consent to ensure refresh token
    }

    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    return AuthUrlResponse(auth_url=auth_url)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = None,
    error: str = None,
    error_description: str = None,
    state: str = None
):
    """
    Handle OAuth callback from Microsoft.

    Returns an HTML page that completes the OAuth flow using
    the stored JWT token from localStorage.
    """
    if error:
        error_msg = error_description or error
        return HTMLResponse(f"""
            <html>
            <body>
                <h1>Microsoft Authorization Failed</h1>
                <p>Error: {error_msg}</p>
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
            <title>Connecting Microsoft - Seny</title>
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
                    border-top: 3px solid #0078D4;
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
                <h1>Connecting Microsoft...</h1>
                <div class="spinner" id="spinner"></div>
                <p id="status">Please wait while we complete the connection.</p>
            </div>
            <script>
                const code = "{code}";
                const TOKEN_KEY = 'seny_access_token';

                async function completeOAuth() {{
                    const token = localStorage.getItem(TOKEN_KEY);
                    const statusEl = document.getElementById('status');
                    const spinnerEl = document.getElementById('spinner');

                    if (!token) {{
                        statusEl.innerHTML = '<span class="error">Not logged in. Please <a href="/login.html">log in</a> first, then try connecting Microsoft again.</span>';
                        spinnerEl.style.display = 'none';
                        return;
                    }}

                    try {{
                        const response = await fetch('/api/microsoft/oauth/complete?code=' + encodeURIComponent(code), {{
                            method: 'POST',
                            headers: {{
                                'Authorization': 'Bearer ' + token,
                                'Content-Type': 'application/json'
                            }}
                        }});

                        const data = await response.json();

                        if (response.ok && data.success) {{
                            statusEl.innerHTML = '<span class="success">✓ Microsoft connected successfully!</span><br><br>Redirecting to Seny...';
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
    user_id: str = Depends(require_auth)
):
    """
    Complete OAuth flow with authorization code.

    Exchanges the code for tokens, gets the user's email,
    and stores the tokens in the database.
    """
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Microsoft OAuth not configured"
        )

    redirect_uri = get_redirect_uri(request)

    try:
        # Exchange code for tokens
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "client_id": MICROSOFT_CLIENT_ID,
                    "client_secret": MICROSOFT_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": " ".join(MICROSOFT_SCOPES)
                }
            )

            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get("error_description", error_data.get("error", "Unknown error"))
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Token exchange failed: {error_msg}"
                )

            token_data = response.json()

        # Get user's email from Microsoft Graph
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {token_data['access_token']}"}
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Failed to get user info from Microsoft"
                )

            user_info = response.json()
            email = user_info.get("mail") or user_info.get("userPrincipalName")

            if not email:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Could not determine email address"
                )

        # Determine account type
        # Work/school accounts have @tenant in userPrincipalName
        # Personal accounts usually have @outlook.com, @hotmail.com, etc.
        upn = user_info.get("userPrincipalName", "")
        if "@" in upn:
            domain = upn.split("@")[1].lower()
            if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
                account_type = "personal"
            else:
                account_type = "work"
        else:
            account_type = "unknown"

        # Calculate expiry
        expires_in = token_data.get("expires_in", 3600)
        expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat() + "Z"

        # Save tokens to database
        save_microsoft_token(
            user_id=int(user_id),
            email=email,
            token_data={
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "scopes": token_data.get("scope", " ".join(MICROSOFT_SCOPES)),
                "expiry": expiry,
                "account_type": account_type
            }
        )

        return OAuthCompleteResponse(
            success=True,
            message=f"Microsoft account {email} connected successfully",
            email=email
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OAuth completion failed: {str(e)}"
        )


@router.get("/accounts", response_model=MicrosoftAccountsResponse)
async def list_accounts(user_id: str = Depends(require_auth)):
    """
    List all connected Microsoft accounts for the current user.
    """
    accounts = list_microsoft_tokens(int(user_id))
    return MicrosoftAccountsResponse(
        accounts=[
            MicrosoftAccount(
                email=acc["email"],
                account_type=acc.get("account_type", "unknown"),
                created_at=acc["created_at"]
            )
            for acc in accounts
        ]
    )


@router.get("/status", response_model=MicrosoftStatusResponse)
async def get_status(
    user_id: str = Depends(require_auth),
    email: str = Query(None, description="Specific Microsoft account to check")
):
    """
    Check Microsoft connection status.

    If email is provided, checks that specific account.
    Otherwise, checks if any Microsoft account is connected.
    """
    token = get_microsoft_token(int(user_id), email)
    if token:
        return MicrosoftStatusResponse(
            connected=True,
            email=token["email"],
            account_type=token.get("account_type", "unknown")
        )
    return MicrosoftStatusResponse(connected=False)


@router.delete("/disconnect", response_model=DisconnectResponse)
async def disconnect(
    user_id: str = Depends(require_auth),
    email: str = Query(..., description="Microsoft account email to disconnect")
):
    """
    Remove a Microsoft account connection.
    """
    success = delete_microsoft_token(int(user_id), email)
    if success:
        return DisconnectResponse(
            success=True,
            message=f"Disconnected {email}"
        )
    return DisconnectResponse(
        success=False,
        message=f"No connection found for {email}"
    )


# ============================================================================
# Email Endpoints (for UI)
# ============================================================================

def _normalize_outlook_summary(msg: dict, account: str = None) -> dict:
    """Normalize Outlook email summary to match Gmail EmailSummary shape."""
    # Build "Name <email>" from_name and from fields
    from_name = msg.get("from_name", "")
    from_addr = msg.get("from", "")
    if from_name and from_addr:
        from_display = f"{from_name} <{from_addr}>"
    else:
        from_display = from_addr or from_name or "Unknown"

    return {
        "id": msg["id"],
        "from_": from_display,
        "subject": msg.get("subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
        "date": msg.get("date", ""),
        "is_unread": not msg.get("isRead", False),
        "account": account,
        "provider": "outlook",
    }


def _normalize_outlook_detail(msg: dict) -> dict:
    """Normalize Outlook email detail to match Gmail EmailDetail shape."""
    from_name = msg.get("from_name", "")
    from_addr = msg.get("from", "")
    if from_name and from_addr:
        from_display = f"{from_name} <{from_addr}>"
    else:
        from_display = from_addr or from_name or "Unknown"

    # Prefer HTML body, fall back to plain text wrapped in <pre>
    body_html = msg.get("body_html") or ""
    body_text = msg.get("body_text") or ""
    if body_html:
        body = body_html
    elif body_text:
        body = f"<pre style='white-space:pre-wrap;font-family:inherit'>{body_text}</pre>"
    else:
        body = ""

    if len(body) > 50000:
        body = body[:50000] + "\n\n[Email truncated - too long to display]"

    # Normalize attachments
    attachments = []
    for att in msg.get("attachments", []):
        attachments.append({
            "filename": att.get("name", att.get("filename", "attachment")),
            "mimeType": att.get("contentType", att.get("mimeType", "")),
            "size": att.get("size", 0),
        })

    return {
        "id": msg["id"],
        "from_": from_display,
        "to": msg.get("to", ""),
        "subject": msg.get("subject", ""),
        "date": msg.get("date", ""),
        "body": body,
        "attachments": attachments,
        "is_unread": not msg.get("isRead", False),
        "provider": "outlook",
    }


@router.get("/inbox")
async def get_inbox(
    user_id: str = Depends(require_auth),
    email: str = Query(None, description="Microsoft account to use"),
    max_results: int = Query(20, ge=1, le=100)
):
    """
    Get inbox emails for the email sidebar (normalized to Gmail shape).
    """
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Microsoft account not connected"
        )

    raw_emails = await outlook.get_inbox(max_results)
    account_email = outlook.email or email
    emails = [_normalize_outlook_summary(e, account_email) for e in raw_emails]
    return {"emails": emails, "account": account_email}


@router.get("/inbox/all")
async def get_unified_inbox(
    user_id: str = Depends(require_auth),
    max_results: int = Query(20, ge=1, le=100)
):
    """
    Get inbox emails from ALL connected Microsoft accounts, normalized and merged.
    """
    accounts = list_microsoft_tokens(int(user_id))
    if not accounts:
        return {"emails": [], "account": "All"}

    account_emails = [acc["email"] for acc in accounts]

    async def fetch_account(acct_email: str) -> list[dict]:
        try:
            outlook = OutlookService(int(user_id), acct_email)
            if not outlook.is_connected():
                return []
            raw = await outlook.get_inbox(max_results)
            return [_normalize_outlook_summary(e, acct_email) for e in raw]
        except Exception as e:
            print(f"[Microsoft] Error fetching inbox from {acct_email}: {e}")
            return []

    all_lists = await asyncio.gather(*[fetch_account(e) for e in account_emails])
    all_emails = [email for sublist in all_lists for email in sublist]

    # Sort by date descending
    def parse_date(e: dict) -> float:
        try:
            date_str = e.get("date", "")
            # Try ISO format first (Outlook uses this)
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp()
        except Exception:
            try:
                return parsedate_to_datetime(date_str).timestamp()
            except Exception:
                return 0

    all_emails.sort(key=parse_date, reverse=True)
    all_emails = all_emails[:max_results * 2]

    return {"emails": all_emails, "account": "All"}


@router.get("/message/{message_id}")
async def get_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None, description="Microsoft account to use")
):
    """
    Get full email content by ID (normalized to Gmail shape).
    """
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Microsoft account not connected"
        )

    message = await outlook.read_email(message_id)
    if not message:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found"
        )

    return _normalize_outlook_detail(message)


@router.post("/message/{message_id}/archive")
async def archive_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """Archive an email."""
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    success = await outlook.archive_email(message_id)
    return {"success": success}


@router.post("/message/{message_id}/trash")
async def trash_message(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """Move email to trash."""
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    success = await outlook.delete_email(message_id)
    return {"success": success}


@router.post("/message/{message_id}/read")
async def mark_read(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """Mark email as read."""
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    success = await outlook.mark_read(message_id)
    return {"success": success}


@router.post("/message/{message_id}/unread")
async def mark_unread(
    message_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """Mark email as unread."""
    outlook = OutlookService(int(user_id), email)
    if not outlook.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    success = await outlook.mark_unread(message_id)
    return {"success": success}


# ============================================================================
# Calendar Endpoints (for UI)
# ============================================================================

@router.get("/calendars")
async def list_calendars(
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """List all calendars for the user."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    calendars = await calendar.list_calendars()
    return {"calendars": calendars}


@router.get("/events")
async def get_events(
    user_id: str = Depends(require_auth),
    email: str = Query(None),
    calendar_id: str = Query(None),
    start_date: str = Query(None),
    end_date: str = Query(None),
    days_ahead: int = Query(7, ge=1, le=365),
    max_results: int = Query(50, ge=1, le=100),
    timezone: str = Query("UTC")
):
    """Get calendar events."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    events = await calendar.get_events(
        calendar_id=calendar_id,
        days_ahead=days_ahead,
        start_date=start_date,
        end_date=end_date,
        max_results=max_results,
        timezone=timezone
    )
    return {"events": events}


@router.get("/event/{event_id}")
async def get_event(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None),
    calendar_id: str = Query(None)
):
    """Get a single calendar event."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    event = await calendar.get_event(event_id, calendar_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return event


class CreateEventRequest(BaseModel):
    summary: str
    start: str
    end: str
    description: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[list[str]] = None
    calendar_id: Optional[str] = None
    timezone: str = "UTC"
    is_all_day: bool = False


@router.post("/event")
async def create_event(
    data: CreateEventRequest,
    user_id: str = Depends(require_auth),
    email: str = Query(None)
):
    """Create a calendar event."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    event = await calendar.create_event(
        summary=data.summary,
        start_time=data.start,
        end_time=data.end,
        description=data.description,
        location=data.location,
        attendees=data.attendees,
        calendar_id=data.calendar_id,
        timezone=data.timezone,
        is_all_day=data.is_all_day
    )

    if not event:
        raise HTTPException(status_code=500, detail="Failed to create event")

    return event


class UpdateEventRequest(BaseModel):
    summary: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    attendees: Optional[list[str]] = None
    timezone: str = "UTC"


@router.put("/event/{event_id}")
async def update_event(
    event_id: str,
    data: UpdateEventRequest,
    user_id: str = Depends(require_auth),
    email: str = Query(None),
    calendar_id: str = Query(None)
):
    """Update a calendar event."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    event = await calendar.update_event(
        event_id=event_id,
        summary=data.summary,
        start_time=data.start,
        end_time=data.end,
        description=data.description,
        location=data.location,
        attendees=data.attendees,
        calendar_id=calendar_id,
        timezone=data.timezone
    )

    if not event:
        raise HTTPException(status_code=500, detail="Failed to update event")

    return event


@router.delete("/event/{event_id}")
async def delete_event(
    event_id: str,
    user_id: str = Depends(require_auth),
    email: str = Query(None),
    calendar_id: str = Query(None)
):
    """Delete a calendar event."""
    calendar = OutlookCalendarService(int(user_id), email)
    if not calendar.is_connected():
        raise HTTPException(status_code=400, detail="Microsoft account not connected")

    success = await calendar.delete_event(event_id, calendar_id)
    return {"success": success}
