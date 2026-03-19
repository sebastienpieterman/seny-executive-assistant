"""
Microsoft Outlook service wrapper for Seny.

Provides Outlook email access via Microsoft Graph API with automatic token refresh:
- Load credentials from database
- Auto-refresh expired tokens (Microsoft tokens expire in 1 hour)
- Execute Microsoft Graph API calls for email operations

Usage:
    outlook = OutlookService(user_id, email)
    if outlook.is_connected():
        emails = await outlook.search_emails("from:boss@company.com")
"""

import os
import re
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from html import unescape

import httpx

from web.core.database import (
    get_microsoft_token,
    save_microsoft_token,
    list_microsoft_tokens,
    update_microsoft_token
)

logger = logging.getLogger(__name__)


# OAuth Configuration
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")

# Microsoft Graph API base URL
GRAPH_API_URL = "https://graph.microsoft.com/v1.0"

# Token endpoint (using /common/ for both personal and work accounts)
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


# ---------------------------------------------------------------------------
# Token refresh circuit breaker
# Prevents infinite retry storms when an Outlook token is revoked.
# Key: "user_id:email"
# ---------------------------------------------------------------------------
_outlook_token_circuit: dict[str, dict] = {}
_TOKEN_CIRCUIT_THRESHOLD = 3          # failures before opening
_TOKEN_CIRCUIT_RECOVERY_SECONDS = 3600  # 1-hour cooldown


def _check_token_circuit(user_id: int, email: str) -> bool:
    """Return True if circuit is open (refresh should be skipped)."""
    key = f"{user_id}:{email}"
    state = _outlook_token_circuit.get(key)
    if not state:
        return False
    if state["failures"] < _TOKEN_CIRCUIT_THRESHOLD:
        return False
    elapsed = time.time() - state["opened_at"]
    if elapsed >= _TOKEN_CIRCUIT_RECOVERY_SECONDS:
        _outlook_token_circuit.pop(key, None)
        return False
    return True  # Circuit open


def _record_token_failure(user_id: int, email: str, error: Exception) -> None:
    """Increment failure count; open circuit after threshold."""
    key = f"{user_id}:{email}"
    state = _outlook_token_circuit.setdefault(key, {"failures": 0, "opened_at": None})
    state["failures"] += 1
    failure_count = state["failures"]
    if failure_count >= _TOKEN_CIRCUIT_THRESHOLD:
        state["opened_at"] = time.time()
        logger.warning(
            "Token circuit open for %s (user %d) — skipping refresh for %d min",
            email, user_id, _TOKEN_CIRCUIT_RECOVERY_SECONDS // 60
        )
        if failure_count == _TOKEN_CIRCUIT_THRESHOLD:
            from web.services.integration_alerts import schedule_token_alert
            schedule_token_alert(user_id, "outlook", email)
    else:
        logger.error(
            "Token refresh failed for %s (user %d): %s — circuit failure %d/%d",
            email, user_id, repr(error), failure_count, _TOKEN_CIRCUIT_THRESHOLD
        )


def _reset_token_circuit(user_id: int, email: str) -> None:
    """Reset circuit after a successful token refresh."""
    _outlook_token_circuit.pop(f"{user_id}:{email}", None)


class OutlookService:
    """
    Microsoft Outlook email service using Graph API.

    Handles OAuth token refresh and Microsoft Graph API calls.
    One instance per user/email combination - do not share across requests.

    Attributes:
        user_id: The user's database ID
        email: The Microsoft account email to use
    """

    def __init__(self, user_id: int, email: str = None):
        """
        Initialize Outlook service for a specific user and email account.

        Args:
            user_id: User's database ID
            email: Microsoft account email (if None, uses first connected account)
        """
        self.user_id = user_id
        self.email = email
        self._token_data: Optional[dict] = None
        self._client: Optional[httpx.AsyncClient] = None

    def is_connected(self) -> bool:
        """
        Check if this user has Microsoft credentials stored.

        Returns:
            True if Microsoft tokens exist for this user (and email if specified)
        """
        token_data = get_microsoft_token(self.user_id, self.email)
        return token_data is not None

    @staticmethod
    def list_connected_accounts(user_id: int) -> list[dict]:
        """
        List all Microsoft accounts connected for a user.

        Args:
            user_id: User's database ID

        Returns:
            List of connected account info (email, account_type, created_at)
        """
        return list_microsoft_tokens(user_id)

    async def get_credentials(self) -> Optional[dict]:
        """
        Load credentials from database and refresh if expired.

        Returns:
            Valid token data dict, or None if:
            - No tokens stored
            - Refresh token is invalid/revoked (needs re-authorization)

        Side effect:
            Updates database if tokens were refreshed
        """
        if self._token_data is not None:
            # Check if still valid (not expired)
            expiry = self._token_data.get('expiry')
            if expiry:
                try:
                    expiry_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    # Add 5 minute buffer before expiry
                    if expiry_dt > datetime.now(expiry_dt.tzinfo) + timedelta(minutes=5):
                        return self._token_data
                except (ValueError, TypeError):
                    pass
            # Fall through to refresh

        token_data = get_microsoft_token(self.user_id, self.email)
        if not token_data:
            return None

        # Update email if we got it from first account
        if not self.email:
            self.email = token_data['email']

        self._token_data = token_data

        # Check if token needs refresh
        needs_refresh = False
        expiry = token_data.get('expiry')
        if expiry:
            try:
                expiry_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                # Refresh if expiring within 5 minutes
                if expiry_dt <= datetime.now(expiry_dt.tzinfo) + timedelta(minutes=5):
                    needs_refresh = True
            except (ValueError, TypeError):
                needs_refresh = True  # Invalid expiry format, refresh to be safe
        else:
            needs_refresh = True  # No expiry, refresh to be safe

        if needs_refresh:
            refreshed = await self._refresh_token()
            if not refreshed:
                self._token_data = None
                return None

        return self._token_data

    async def _refresh_token(self) -> bool:
        """
        Refresh the access token using the refresh token.

        Returns:
            True if refresh succeeded, False if failed (need re-auth)
        """
        if not self._token_data or not self._token_data.get('refresh_token'):
            return False

        # Check circuit breaker before hitting Microsoft's token endpoint
        email = self.email or self._token_data.get('email', 'unknown')
        if _check_token_circuit(self.user_id, email):
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        'client_id': MICROSOFT_CLIENT_ID,
                        'client_secret': MICROSOFT_CLIENT_SECRET,
                        'refresh_token': self._token_data['refresh_token'],
                        'grant_type': 'refresh_token',
                        'scope': self._token_data.get('scopes', '')
                    }
                )

                if response.status_code != 200:
                    err = Exception(f"HTTP {response.status_code}: {response.text[:200]}")
                    _record_token_failure(self.user_id, email, err)
                    return False

                data = response.json()

                # Calculate new expiry
                expires_in = data.get('expires_in', 3600)  # Default 1 hour
                new_expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat() + 'Z'

                # Update token data
                self._token_data['access_token'] = data['access_token']
                self._token_data['expiry'] = new_expiry

                # Update refresh token if a new one was provided
                if 'refresh_token' in data:
                    self._token_data['refresh_token'] = data['refresh_token']

                # Save updated tokens to database
                update_microsoft_token(
                    self.user_id,
                    self.email,
                    data['access_token'],
                    new_expiry
                )

                logger.info(f"Refreshed Microsoft token for {self.email}")
                # Successful refresh — reset any previous failure count
                _reset_token_circuit(self.user_id, email)
                return True

        except Exception as e:
            _record_token_failure(self.user_id, email, e)
            return False

    async def _get_client(self) -> Optional[httpx.AsyncClient]:
        """
        Get or create HTTP client with auth headers.

        Returns:
            Configured httpx client, or None if not connected
        """
        credentials = await self.get_credentials()
        if not credentials:
            return None

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=GRAPH_API_URL,
                headers={
                    'Authorization': f"Bearer {credentials['access_token']}",
                    'Content-Type': 'application/json'
                },
                timeout=30.0
            )

        return self._client

    async def _api_get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """
        Make GET request to Microsoft Graph API.

        Args:
            endpoint: API endpoint (e.g., '/me/messages')
            params: Query parameters

        Returns:
            JSON response or None on error
        """
        client = await self._get_client()
        if not client:
            return None

        try:
            # Ensure endpoint starts with /
            if not endpoint.startswith('/'):
                endpoint = '/' + endpoint

            response = await client.get(endpoint, params=params)

            if response.status_code == 401:
                # Token may have expired mid-request — check circuit before retrying
                email = self.email or ''
                if _check_token_circuit(self.user_id, email):
                    logger.error(
                        "Graph API GET %s: 401 and circuit open for %s — skipping retry",
                        endpoint, email
                    )
                    return None
                self._token_data = None
                self._client = None
                client = await self._get_client()
                if client:
                    response = await client.get(endpoint, params=params)

            if response.status_code != 200:
                logger.error(f"Graph API GET {endpoint} failed: {response.status_code} - {response.text[:500]}")
                return None

            return response.json()

        except Exception as e:
            logger.error(f"Graph API GET error: {e}")
            return None

    async def _api_post(self, endpoint: str, json_data: dict = None) -> Optional[dict]:
        """
        Make POST request to Microsoft Graph API.

        Args:
            endpoint: API endpoint
            json_data: JSON body

        Returns:
            JSON response or None on error
        """
        client = await self._get_client()
        if not client:
            return None

        try:
            if not endpoint.startswith('/'):
                endpoint = '/' + endpoint

            response = await client.post(endpoint, json=json_data)

            if response.status_code == 401:
                # Check circuit before retrying
                email = self.email or ''
                if _check_token_circuit(self.user_id, email):
                    logger.error(
                        "Graph API POST %s: 401 and circuit open for %s — skipping retry",
                        endpoint, email
                    )
                    return None
                self._token_data = None
                self._client = None
                client = await self._get_client()
                if client:
                    response = await client.post(endpoint, json=json_data)

            # POST can return 200, 201, 202, or 204
            if response.status_code not in (200, 201, 202, 204):
                logger.error(f"Graph API POST {endpoint} failed: {response.status_code} - {response.text[:500]}")
                return None

            if response.status_code == 204:
                return {}  # No content

            return response.json()

        except Exception as e:
            logger.error(f"Graph API POST error: {e}")
            return None

    async def _api_patch(self, endpoint: str, json_data: dict = None) -> Optional[dict]:
        """
        Make PATCH request to Microsoft Graph API.
        """
        client = await self._get_client()
        if not client:
            return None

        try:
            if not endpoint.startswith('/'):
                endpoint = '/' + endpoint

            response = await client.patch(endpoint, json=json_data)

            if response.status_code == 401:
                # Check circuit before retrying
                email = self.email or ''
                if _check_token_circuit(self.user_id, email):
                    logger.error(
                        "Graph API PATCH %s: 401 and circuit open for %s — skipping retry",
                        endpoint, email
                    )
                    return None
                self._token_data = None
                self._client = None
                client = await self._get_client()
                if client:
                    response = await client.patch(endpoint, json=json_data)

            if response.status_code not in (200, 204):
                logger.error(f"Graph API PATCH {endpoint} failed: {response.status_code} - {response.text[:500]}")
                return None

            if response.status_code == 204:
                return {}

            return response.json()

        except Exception as e:
            logger.error(f"Graph API PATCH error: {e}")
            return None

    async def _api_delete(self, endpoint: str) -> bool:
        """
        Make DELETE request to Microsoft Graph API.

        Returns:
            True if successful, False otherwise
        """
        client = await self._get_client()
        if not client:
            return False

        try:
            if not endpoint.startswith('/'):
                endpoint = '/' + endpoint

            response = await client.delete(endpoint)

            if response.status_code == 401:
                # Check circuit before retrying
                email = self.email or ''
                if _check_token_circuit(self.user_id, email):
                    logger.error(
                        "Graph API DELETE %s: 401 and circuit open for %s — skipping retry",
                        endpoint, email
                    )
                    return False
                self._token_data = None
                self._client = None
                client = await self._get_client()
                if client:
                    response = await client.delete(endpoint)

            return response.status_code in (200, 204)

        except Exception as e:
            logger.error(f"Graph API DELETE error: {e}")
            return False

    def _strip_html(self, html: str) -> str:
        """
        Strip HTML tags and convert to plain text.

        Args:
            html: HTML string

        Returns:
            Plain text string
        """
        if not html:
            return ""

        # Remove script and style elements
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Replace br and p tags with newlines
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<p[^>]*>', '', text, flags=re.IGNORECASE)

        # Remove remaining tags
        text = re.sub(r'<[^>]+>', '', text)

        # Decode HTML entities
        text = unescape(text)

        # Clean up whitespace
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = text.strip()

        return text

    async def search_emails(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Search Outlook emails.

        Microsoft Graph supports $search for keyword search and $filter for field-based queries.
        This method uses $search for natural language queries.

        Args:
            query: Search query (keywords, sender names, subjects, etc.)
            max_results: Maximum emails to return (default 10, max 50)

        Returns:
            List of email summaries with: id, from, to, subject, snippet, date, isRead
        """
        max_results = min(max(1, max_results), 50)

        # Use $search for keyword queries
        # Note: $search requires ConsistencyLevel header
        params = {
            '$search': f'"{query}"',
            '$top': max_results,
            '$select': 'id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead',
            '$orderby': 'receivedDateTime desc'
        }

        # Make request with ConsistencyLevel header
        client = await self._get_client()
        if not client:
            return []

        try:
            response = await client.get(
                '/me/messages',
                params=params,
                headers={'ConsistencyLevel': 'eventual'}
            )

            if response.status_code != 200:
                # $search can fail on some tenants, fall back to $filter
                logger.warning(f"$search failed, trying $filter: {response.status_code}")
                return await self._search_with_filter(query, max_results)

            data = response.json()
            messages = data.get('value', [])

            return [
                {
                    'id': msg['id'],
                    'from': msg.get('from', {}).get('emailAddress', {}).get('address', ''),
                    'from_name': msg.get('from', {}).get('emailAddress', {}).get('name', ''),
                    'to': ', '.join(
                        r.get('emailAddress', {}).get('address', '')
                        for r in msg.get('toRecipients', [])
                    ),
                    'subject': msg.get('subject', '(no subject)'),
                    'snippet': msg.get('bodyPreview', '')[:200],
                    'date': msg.get('receivedDateTime', ''),
                    'isRead': msg.get('isRead', False)
                }
                for msg in messages
            ]

        except Exception as e:
            logger.error(f"Outlook search failed: {e}")
            return []

    async def _search_with_filter(self, query: str, max_results: int) -> list[dict]:
        """
        Fallback search using $filter instead of $search.
        Less powerful but works on all tenants.
        """
        # Simple filter - search subject contains query
        params = {
            '$filter': f"contains(subject, '{query}')",
            '$top': max_results,
            '$select': 'id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead',
            '$orderby': 'receivedDateTime desc'
        }

        result = await self._api_get('/me/messages', params)
        if not result:
            return []

        messages = result.get('value', [])

        return [
            {
                'id': msg['id'],
                'from': msg.get('from', {}).get('emailAddress', {}).get('address', ''),
                'from_name': msg.get('from', {}).get('emailAddress', {}).get('name', ''),
                'to': ', '.join(
                    r.get('emailAddress', {}).get('address', '')
                    for r in msg.get('toRecipients', [])
                ),
                'subject': msg.get('subject', '(no subject)'),
                'snippet': msg.get('bodyPreview', '')[:200],
                'date': msg.get('receivedDateTime', ''),
                'isRead': msg.get('isRead', False)
            }
            for msg in messages
        ]

    async def get_inbox(self, max_results: int = 20) -> list[dict]:
        """
        Get recent inbox messages.

        Args:
            max_results: Maximum emails to return (default 20)

        Returns:
            List of email summaries
        """
        max_results = min(max(1, max_results), 100)

        params = {
            '$top': max_results,
            '$select': 'id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead,hasAttachments',
            '$orderby': 'receivedDateTime desc',
            '$filter': "isDraft eq false"
        }

        result = await self._api_get('/me/mailFolders/inbox/messages', params)
        if not result:
            return []

        messages = result.get('value', [])

        return [
            {
                'id': msg['id'],
                'from': msg.get('from', {}).get('emailAddress', {}).get('address', ''),
                'from_name': msg.get('from', {}).get('emailAddress', {}).get('name', ''),
                'to': ', '.join(
                    r.get('emailAddress', {}).get('address', '')
                    for r in msg.get('toRecipients', [])
                ),
                'subject': msg.get('subject', '(no subject)'),
                'snippet': msg.get('bodyPreview', '')[:200],
                'date': msg.get('receivedDateTime', ''),
                'isRead': msg.get('isRead', False),
                'hasAttachments': msg.get('hasAttachments', False)
            }
            for msg in messages
        ]

    async def read_email(self, message_id: str) -> Optional[dict]:
        """
        Get full email content by message ID.

        Args:
            message_id: Outlook message ID

        Returns:
            Email dict with full content, or None if not found
        """
        result = await self._api_get(f'/me/messages/{message_id}')
        if not result:
            return None

        # Extract body - prefer text, fall back to HTML converted to text
        body = result.get('body', {})
        if body.get('contentType') == 'text':
            body_text = body.get('content', '')
        else:
            # HTML - convert to text
            body_text = self._strip_html(body.get('content', ''))

        # Get attachments metadata
        attachments = []
        if result.get('hasAttachments'):
            att_result = await self._api_get(f'/me/messages/{message_id}/attachments')
            if att_result:
                for att in att_result.get('value', []):
                    attachments.append({
                        'id': att.get('id'),
                        'name': att.get('name'),
                        'contentType': att.get('contentType'),
                        'size': att.get('size')
                    })

        return {
            'id': result['id'],
            'conversationId': result.get('conversationId'),
            'from': result.get('from', {}).get('emailAddress', {}).get('address', ''),
            'from_name': result.get('from', {}).get('emailAddress', {}).get('name', ''),
            'to': ', '.join(
                r.get('emailAddress', {}).get('address', '')
                for r in result.get('toRecipients', [])
            ),
            'cc': ', '.join(
                r.get('emailAddress', {}).get('address', '')
                for r in result.get('ccRecipients', [])
            ),
            'subject': result.get('subject', ''),
            'date': result.get('receivedDateTime', ''),
            'body_text': body_text,
            'body_html': body.get('content', '') if body.get('contentType') == 'html' else None,
            'isRead': result.get('isRead', False),
            'hasAttachments': result.get('hasAttachments', False),
            'attachments': attachments
        }

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = None,
        bcc: str = None,
        reply_to_message_id: str = None
    ) -> Optional[dict]:
        """
        Send an email via Outlook.

        Args:
            to: Recipient email address(es), comma-separated for multiple
            subject: Email subject line
            body: Email body (plain text)
            cc: CC recipients (optional), comma-separated
            bcc: BCC recipients (optional), comma-separated
            reply_to_message_id: Outlook message ID to reply to (optional)

        Returns:
            Dict with sent message info or None if failed
        """
        # Build message object
        message = {
            'subject': subject,
            'body': {
                'contentType': 'text',
                'content': body
            },
            'toRecipients': [
                {'emailAddress': {'address': addr.strip()}}
                for addr in to.split(',')
            ]
        }

        if cc:
            message['ccRecipients'] = [
                {'emailAddress': {'address': addr.strip()}}
                for addr in cc.split(',')
            ]

        if bcc:
            message['bccRecipients'] = [
                {'emailAddress': {'address': addr.strip()}}
                for addr in bcc.split(',')
            ]

        # If replying, we need to use the reply endpoint
        if reply_to_message_id:
            # Use reply endpoint
            reply_data = {
                'message': message,
                'comment': body  # The reply body
            }

            result = await self._api_post(
                f'/me/messages/{reply_to_message_id}/reply',
                reply_data
            )

            if result is not None:
                return {'status': 'sent', 'reply_to': reply_to_message_id}
            return None

        # Regular send
        result = await self._api_post('/me/sendMail', {'message': message})

        if result is not None:
            return {'status': 'sent'}
        return None

    async def mark_read(self, message_id: str) -> bool:
        """
        Mark a message as read.

        Args:
            message_id: Outlook message ID

        Returns:
            True if successful
        """
        result = await self._api_patch(
            f'/me/messages/{message_id}',
            {'isRead': True}
        )
        return result is not None

    async def mark_unread(self, message_id: str) -> bool:
        """
        Mark a message as unread.

        Args:
            message_id: Outlook message ID

        Returns:
            True if successful
        """
        result = await self._api_patch(
            f'/me/messages/{message_id}',
            {'isRead': False}
        )
        return result is not None

    async def archive_email(self, message_id: str) -> bool:
        """
        Move email to Archive folder.

        Args:
            message_id: Outlook message ID

        Returns:
            True if successful
        """
        # Get archive folder ID (or create if doesn't exist)
        folders = await self._api_get('/me/mailFolders')
        if not folders:
            return False

        archive_id = None
        for folder in folders.get('value', []):
            if folder.get('displayName', '').lower() == 'archive':
                archive_id = folder['id']
                break

        if not archive_id:
            # Archive folder doesn't exist - use wellKnownName
            result = await self._api_get('/me/mailFolders/archive')
            if result:
                archive_id = result.get('id')

        if not archive_id:
            logger.warning("Could not find Archive folder")
            return False

        # Move message to archive
        result = await self._api_post(
            f'/me/messages/{message_id}/move',
            {'destinationId': archive_id}
        )
        return result is not None

    async def delete_email(self, message_id: str) -> bool:
        """
        Move email to Deleted Items folder.

        Args:
            message_id: Outlook message ID

        Returns:
            True if successful
        """
        # Move to deleted items folder
        result = await self._api_post(
            f'/me/messages/{message_id}/move',
            {'destinationId': 'deleteditems'}
        )
        return result is not None

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
