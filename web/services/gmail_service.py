"""
Gmail API service wrapper for Seny.

Provides Gmail access with automatic token refresh:
- Load credentials from database
- Auto-refresh expired tokens
- Build Gmail API service object

Usage:
    gmail = GmailService(user_id, email)
    if gmail.is_connected():
        service = await gmail.get_service()
        # Use service for Gmail API calls
"""

import os
import base64
import logging
import time
from datetime import datetime
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError

from web.core.database import get_gmail_token, save_gmail_token, list_gmail_tokens

logger = logging.getLogger(__name__)


# OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")


# ---------------------------------------------------------------------------
# Token refresh circuit breaker
# Prevents infinite retry storms when a Google token is revoked.
# Key: "user_id:email"
# ---------------------------------------------------------------------------
_token_circuit: dict[str, dict] = {}
_TOKEN_CIRCUIT_THRESHOLD = 3          # failures before opening
_TOKEN_CIRCUIT_RECOVERY_SECONDS = 3600  # 1-hour cooldown


def _check_token_circuit(user_id: int, email: str) -> bool:
    """Return True if circuit is open (refresh should be skipped)."""
    key = f"{user_id}:{email}"
    state = _token_circuit.get(key)
    if not state:
        return False
    if state["failures"] < _TOKEN_CIRCUIT_THRESHOLD:
        return False
    elapsed = time.time() - state["opened_at"]
    if elapsed >= _TOKEN_CIRCUIT_RECOVERY_SECONDS:
        # Recovery window passed — reset circuit so we try again
        _token_circuit.pop(key, None)
        return False
    return True  # Circuit open


def _record_token_failure(user_id: int, email: str, error: Exception) -> None:
    """Increment failure count; open circuit after threshold."""
    key = f"{user_id}:{email}"
    state = _token_circuit.setdefault(key, {"failures": 0, "opened_at": None})
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
            schedule_token_alert(user_id, "gmail", email)
    else:
        logger.error(
            "Token refresh failed for %s (user %d): %s — circuit failure %d/%d",
            email, user_id, repr(error), failure_count, _TOKEN_CIRCUIT_THRESHOLD
        )


def _reset_token_circuit(user_id: int, email: str) -> None:
    """Reset circuit after a successful token refresh."""
    _token_circuit.pop(f"{user_id}:{email}", None)


class GmailService:
    """
    Gmail API wrapper with automatic credential management.

    Handles OAuth token refresh and Gmail API service creation.
    One instance per user/email combination - do not share across requests.

    Attributes:
        user_id: The user's database ID
        email: The Gmail address to use
    """

    def __init__(self, user_id: int, email: str):
        """
        Initialize Gmail service for a specific user and email account.

        Args:
            user_id: User's database ID
            email: Gmail address to use for API calls
        """
        self.user_id = user_id
        self.email = email
        self._service: Optional[Resource] = None
        self._credentials: Optional[Credentials] = None

    def is_connected(self) -> bool:
        """
        Check if this email has Gmail credentials stored.

        Note: This only checks if tokens exist, not if they're valid.
        Use get_credentials() to verify tokens are valid/refreshable.

        Returns:
            True if this email has Gmail tokens stored
        """
        token_data = get_gmail_token(self.user_id, self.email)
        return token_data is not None

    @staticmethod
    def list_connected_accounts(user_id: int) -> list[dict]:
        """
        List all Gmail accounts connected for a user.

        Args:
            user_id: User's database ID

        Returns:
            List of connected account info (email, created_at)
        """
        return list_gmail_tokens(user_id)

    async def get_credentials(self) -> Optional[Credentials]:
        """
        Load credentials from database and refresh if expired.

        Returns:
            Valid Credentials object, or None if:
            - No tokens stored for this email
            - Refresh token is invalid/revoked (needs re-authorization)

        Side effect:
            Updates database if tokens were refreshed
        """
        if self._credentials is not None:
            # Already loaded this request
            if not self._credentials.expired:
                return self._credentials
            # Fall through to refresh

        token_data = get_gmail_token(self.user_id, self.email)
        if not token_data:
            return None

        # Parse expiry string to datetime if present
        expiry = None
        if token_data["expiry"]:
            try:
                expiry = datetime.fromisoformat(token_data["expiry"])
            except ValueError:
                pass  # Invalid expiry format, will trigger refresh

        # Build Credentials object from stored data
        self._credentials = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            token_uri=token_data["token_uri"],
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=token_data["scopes"].split(",") if token_data["scopes"] else None,
            expiry=expiry
        )

        # Refresh if expired
        if self._credentials.expired and self._credentials.refresh_token:
            # Check circuit breaker before hitting Google's OAuth server
            if _check_token_circuit(self.user_id, self.email):
                self._credentials = None
                return None
            try:
                self._credentials.refresh(Request())
                # Save refreshed tokens back to database
                save_gmail_token(self.user_id, self.email, self._credentials)
                # Successful refresh — reset any previous failure count
                _reset_token_circuit(self.user_id, self.email)
            except Exception as e:
                # Refresh failed - token may be revoked
                # Testing mode tokens expire after 7 days
                # User needs to re-authorize
                _record_token_failure(self.user_id, self.email, e)
                self._credentials = None
                return None

        return self._credentials

    async def get_service(self) -> Optional[Resource]:
        """
        Get or create Gmail API service.

        Returns:
            Gmail API service object, or None if not connected/authorized

        Usage:
            service = await gmail.get_service()
            if service:
                messages = service.users().messages().list(userId='me').execute()
        """
        if self._service is not None:
            return self._service

        credentials = await self.get_credentials()
        if credentials is None:
            return None

        # Build Gmail API service
        # Note: build() is synchronous but quick - just creates the service object
        self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    def _execute_with_backoff(self, request, max_retries: int = 3):
        """
        Execute Gmail API request with exponential backoff for rate limits.

        Args:
            request: Gmail API request object
            max_retries: Maximum number of retry attempts

        Returns:
            API response or None if all retries failed
        """
        for attempt in range(max_retries):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 503):
                    # Rate limit or server error - retry with backoff
                    wait_time = (2 ** attempt) + (time.time() % 1)
                    logger.warning(f"Gmail API error {e.resp.status}, retrying in {wait_time:.1f}s")
                    time.sleep(wait_time)
                else:
                    # Other HTTP error - don't retry
                    logger.error(f"Gmail API error: {e}")
                    raise
        return None

    async def search_emails(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Search emails using Gmail query syntax.

        Query examples:
        - "from:john@example.com" - from specific sender
        - "is:unread" - unread messages
        - "subject:meeting" - subject contains word
        - "after:2025/01/01" - after date
        - "has:attachment" - has attachments
        - "in:inbox" - in inbox only

        Args:
            query: Gmail search query (same syntax as Gmail web search)
            max_results: Maximum emails to return (default 10, max 50)

        Returns:
            List of email summaries with: id, threadId, from, to, subject, snippet, date
        """
        service = await self.get_service()
        if not service:
            return []

        # Clamp max_results
        max_results = min(max(1, max_results), 50)

        try:
            # Get message IDs matching query
            results = self._execute_with_backoff(
                service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=max_results
                )
            )

            if not results:
                return []

            messages = results.get('messages', [])
            summaries = []

            for msg in messages:
                try:
                    # Get metadata for each message (not full content)
                    msg_data = self._execute_with_backoff(
                        service.users().messages().get(
                            userId='me',
                            id=msg['id'],
                            format='metadata',
                            metadataHeaders=['From', 'To', 'Subject', 'Date']
                        )
                    )

                    if not msg_data:
                        continue

                    headers = {
                        h['name'].lower(): h['value']
                        for h in msg_data['payload'].get('headers', [])
                    }

                    summaries.append({
                        'id': msg_data['id'],
                        'threadId': msg_data['threadId'],
                        'from': headers.get('from', ''),
                        'to': headers.get('to', ''),
                        'subject': headers.get('subject', '(no subject)'),
                        'snippet': msg_data.get('snippet', ''),
                        'date': headers.get('date', ''),
                        'labelIds': msg_data.get('labelIds', [])
                    })
                except HttpError as e:
                    logger.warning(f"Failed to fetch message {msg['id']}: {e}")
                    continue

            return summaries

        except HttpError as e:
            logger.error(f"Gmail search failed: {e}")
            return []

    async def get_message_metadata(self, message_id: str) -> Optional[dict]:
        """
        Fetch basic metadata for a single message.

        Args:
            message_id: Gmail message ID

        Returns:
            Metadata dict with id, threadId, from, to, subject, date, labelIds
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            msg_data = self._execute_with_backoff(
                service.users().messages().get(
                    userId='me',
                    id=message_id,
                    format='metadata',
                    metadataHeaders=['From', 'To', 'Subject', 'Date']
                )
            )

            if not msg_data:
                return None

            headers = {
                h['name'].lower(): h['value']
                for h in msg_data['payload'].get('headers', [])
            }

            return {
                'id': msg_data.get('id', ''),
                'threadId': msg_data.get('threadId', ''),
                'from': headers.get('from', ''),
                'to': headers.get('to', ''),
                'subject': headers.get('subject', '(no subject)'),
                'date': headers.get('date', ''),
                'labelIds': msg_data.get('labelIds', [])
            }
        except HttpError as e:
            logger.warning(f"Failed to fetch metadata for message {message_id}: {e}")
            return None

    async def search_message_ids(self, query: str, max_results: Optional[int] = None) -> list[str]:
        """
        Search Gmail and return matching message IDs.

        Args:
            query: Gmail search query
            max_results: Maximum number of message IDs to return. If omitted, returns all matching IDs.

        Returns:
            List of Gmail message IDs matching the query.
        """
        service = await self.get_service()
        if not service:
            return []

        message_ids: list[str] = []
        page_token = None

        while True:
            remaining = None if max_results is None else max(1, max_results - len(message_ids))
            page_size = 500 if remaining is None else min(500, remaining)

            params = {
                'userId': 'me',
                'q': query,
                'maxResults': page_size
            }
            if page_token:
                params['pageToken'] = page_token

            results = self._execute_with_backoff(
                service.users().messages().list(**params)
            )

            if not results:
                break

            for msg in results.get('messages', []):
                message_ids.append(msg['id'])
                if max_results is not None and len(message_ids) >= max_results:
                    break

            if max_results is not None and len(message_ids) >= max_results:
                break

            page_token = results.get('nextPageToken')
            if not page_token:
                break

        return message_ids

    async def mark_read_batch(
        self,
        message_ids: Optional[list[str]] = None,
        query: Optional[str] = None,
        user_instruction: Optional[str] = None
    ) -> int:
        """
        Mark multiple Gmail messages as read.

        Args:
            message_ids: Specific message IDs to mark as read
            query: Optional Gmail query to select additional messages
            user_instruction: Original user instruction for logging

        Returns:
            Number of messages successfully marked as read
        """
        if message_ids is None:
            message_ids = []

        if query:
            query_ids = await self.search_message_ids(query)
            message_ids = list(dict.fromkeys(message_ids + query_ids))

        success_count = 0
        for message_id in message_ids:
            if await self.mark_read(message_id):
                metadata = await self.get_message_metadata(message_id)
                subject = metadata.get('subject') if metadata else ''
                sender = metadata.get('from') if metadata else ''
                logger.info(
                    "Gmail mutation: user_id=%s account=%s action=mark_read message_id=%s subject=%s sender=%s query=%s instruction=%s",
                    self.user_id,
                    self.email,
                    message_id,
                    subject,
                    sender,
                    query,
                    user_instruction
                )
                success_count += 1

        return success_count

    async def mark_unread_batch(
        self,
        message_ids: Optional[list[str]] = None,
        query: Optional[str] = None,
        user_instruction: Optional[str] = None
    ) -> int:
        """
        Mark multiple Gmail messages as unread.

        Args:
            message_ids: Specific message IDs to mark as unread
            query: Optional Gmail query to select additional messages
            user_instruction: Original user instruction for logging

        Returns:
            Number of messages successfully marked as unread
        """
        if message_ids is None:
            message_ids = []

        if query:
            query_ids = await self.search_message_ids(query)
            message_ids = list(dict.fromkeys(message_ids + query_ids))

        success_count = 0
        for message_id in message_ids:
            if await self.mark_unread(message_id):
                metadata = await self.get_message_metadata(message_id)
                subject = metadata.get('subject') if metadata else ''
                sender = metadata.get('from') if metadata else ''
                logger.info(
                    "Gmail mutation: user_id=%s account=%s action=mark_unread message_id=%s subject=%s sender=%s query=%s instruction=%s",
                    self.user_id,
                    self.email,
                    message_id,
                    subject,
                    sender,
                    query,
                    user_instruction
                )
                success_count += 1

        return success_count

    async def read_email(self, message_id: str) -> Optional[dict]:
        """
        Get full email content by message ID.

        Args:
            message_id: Gmail message ID (from search_emails results)

        Returns:
            Email dict with: id, threadId, from, to, subject, date,
            body_text, body_html, attachments, labelIds
            Or None if message not found or not connected
        """
        service = await self.get_service()
        if not service:
            return None

        try:
            msg = self._execute_with_backoff(
                service.users().messages().get(
                    userId='me',
                    id=message_id,
                    format='full'
                )
            )

            if not msg:
                return None

            headers = {
                h['name'].lower(): h['value']
                for h in msg['payload'].get('headers', [])
            }

            # Extract body from potentially nested MIME parts
            body_text, body_html = self._extract_body(msg['payload'])

            # Extract attachment info (not content)
            attachments = self._extract_attachments(msg['payload'])

            return {
                'id': msg['id'],
                'threadId': msg['threadId'],
                'from': headers.get('from', ''),
                'to': headers.get('to', ''),
                'subject': headers.get('subject', ''),
                'date': headers.get('date', ''),
                'body_text': body_text,
                'body_html': body_html,
                'attachments': attachments,
                'labelIds': msg.get('labelIds', [])
            }

        except HttpError as e:
            logger.error(f"Failed to read email {message_id}: {e}")
            return None

    async def get_email_thread(
        self, thread_id: str, exclude_message_id: str = None
    ) -> list[dict]:
        """
        Fetch emails in a Gmail thread (for conversation context).

        Returns up to 3 messages (most recent first), excluding the current
        message. Each entry has: id, from, date, subject, snippet.

        Args:
            thread_id: Gmail thread ID (threadId field on any message)
            exclude_message_id: Message ID to exclude (the item being classified)

        Returns:
            List of dicts or empty list on failure
        """
        if not thread_id:
            return []

        try:
            service = await self.get_service()
            thread_data = service.users().threads().get(
                userId='me',
                id=thread_id,
                format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Date']
            ).execute()

            messages = thread_data.get('messages', [])

            results = []
            for msg in reversed(messages):  # most recent first
                msg_id = msg.get('id', '')
                if msg_id == exclude_message_id:
                    continue
                if len(results) >= 3:
                    break

                headers = {
                    h['name']: h['value']
                    for h in msg.get('payload', {}).get('headers', [])
                }
                snippet = (msg.get('snippet', '') or '')[:200]

                results.append({
                    'id': msg_id,
                    'from': headers.get('From', 'unknown'),
                    'date': headers.get('Date', ''),
                    'subject': headers.get('Subject', '(no subject)'),
                    'snippet': snippet,
                })

            return results

        except Exception as e:
            logger.warning(
                "Gmail thread fetch failed for thread %s: %s",
                thread_id, repr(e)
            )
            return []

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = None,
        bcc: str = None,
        reply_to_message_id: str = None,
        html_body: str = None
    ) -> Optional[dict]:
        """
        Send an email via Gmail, optionally as a reply to an existing message.

        Args:
            to: Recipient email address(es), comma-separated for multiple
            subject: Email subject line
            body: Email body (plain text)
            cc: CC recipients (optional), comma-separated for multiple
            bcc: BCC recipients (optional), comma-separated for multiple
            reply_to_message_id: Gmail message ID to reply to (optional)
            html_body: Email body (HTML, optional) - if provided, sends multipart

        Returns:
            Dict with sent message info (id, threadId, labelIds) or None if failed
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        service = await self.get_service()
        if not service:
            return None

        try:
            thread_id = None
            in_reply_to = None
            references = None

            # If replying, get the original message headers
            if reply_to_message_id:
                original = await self.read_email(reply_to_message_id)
                if original:
                    thread_id = original.get('threadId')
                    # Get Message-ID header from original for In-Reply-To
                    msg_data = self._execute_with_backoff(
                        service.users().messages().get(
                            userId='me',
                            id=reply_to_message_id,
                            format='metadata',
                            metadataHeaders=['Message-ID', 'References']
                        )
                    )
                    if msg_data:
                        headers = {
                            h['name'].lower(): h['value']
                            for h in msg_data['payload'].get('headers', [])
                        }
                        in_reply_to = headers.get('message-id', '')
                        references = headers.get('references', '')
                        # Append original Message-ID to References
                        if in_reply_to:
                            references = f"{references} {in_reply_to}".strip()
                    logger.info(f"Replying to thread {thread_id}, In-Reply-To: {in_reply_to}")

            # Create MIME message - multipart if HTML provided
            if html_body:
                message = MIMEMultipart('alternative')
                # Plain text part (fallback)
                text_part = MIMEText(body, 'plain')
                message.attach(text_part)
                # HTML part (preferred)
                html_part = MIMEText(html_body, 'html')
                message.attach(html_part)
            else:
                message = MIMEText(body)

            message['to'] = to
            message['subject'] = subject

            if cc:
                message['cc'] = cc
            if bcc:
                message['bcc'] = bcc

            # Add reply headers if replying
            if in_reply_to:
                message['In-Reply-To'] = in_reply_to
            if references:
                message['References'] = references

            # Encode message
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

            # Build send body - include threadId if replying
            send_body = {'raw': raw}
            if thread_id:
                send_body['threadId'] = thread_id

            # Send via Gmail API
            result = self._execute_with_backoff(
                service.users().messages().send(
                    userId='me',
                    body=send_body
                )
            )

            if result:
                logger.info(f"Email sent successfully: {result.get('id')}, thread: {result.get('threadId')}")
                return {
                    'id': result.get('id'),
                    'threadId': result.get('threadId'),
                    'labelIds': result.get('labelIds', [])
                }
            return None

        except HttpError as e:
            logger.error(f"Failed to send email: {e}")
            return None

    def _extract_body(self, payload: dict) -> tuple[str, str]:
        """
        Extract plain text and HTML body from MIME payload.

        Handles both simple messages and multipart MIME structures.

        Args:
            payload: Gmail message payload dict

        Returns:
            Tuple of (body_text, body_html)
        """
        body_text = ''
        body_html = ''

        # Simple message - body directly in payload
        if 'body' in payload and payload['body'].get('data'):
            data = payload['body']['data']
            try:
                content = base64.urlsafe_b64decode(data).decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                content = base64.urlsafe_b64decode(data).decode('latin-1', errors='replace')

            if payload.get('mimeType') == 'text/plain':
                body_text = content
            elif payload.get('mimeType') == 'text/html':
                body_html = content
            return body_text, body_html

        # Multipart message - recurse through parts
        parts = payload.get('parts', [])
        for part in parts:
            mime_type = part.get('mimeType', '')

            if mime_type == 'text/plain' and not body_text:
                data = part.get('body', {}).get('data', '')
                if data:
                    try:
                        body_text = base64.urlsafe_b64decode(data).decode('utf-8')
                    except (ValueError, UnicodeDecodeError):
                        body_text = base64.urlsafe_b64decode(data).decode('latin-1', errors='replace')

            elif mime_type == 'text/html' and not body_html:
                data = part.get('body', {}).get('data', '')
                if data:
                    try:
                        body_html = base64.urlsafe_b64decode(data).decode('utf-8')
                    except (ValueError, UnicodeDecodeError):
                        body_html = base64.urlsafe_b64decode(data).decode('latin-1', errors='replace')

            elif mime_type.startswith('multipart/'):
                # Nested multipart - recurse
                nested_text, nested_html = self._extract_body(part)
                if not body_text:
                    body_text = nested_text
                if not body_html:
                    body_html = nested_html

        return body_text, body_html

    def _extract_attachments(self, payload: dict) -> list[dict]:
        """
        Extract attachment metadata from MIME payload.

        Args:
            payload: Gmail message payload dict

        Returns:
            List of attachment dicts with: filename, mimeType, size
        """
        attachments = []
        parts = payload.get('parts', [])

        for part in parts:
            filename = part.get('filename', '')
            if filename:  # Has filename = is attachment
                attachments.append({
                    'filename': filename,
                    'mimeType': part.get('mimeType', ''),
                    'size': part.get('body', {}).get('size', 0)
                })
            # Check nested parts
            if part.get('parts'):
                attachments.extend(self._extract_attachments(part))

        return attachments

    async def get_inbox(self, max_results: int = 10) -> list[dict]:
        """
        Get recent inbox emails for UI display.

        Args:
            max_results: Maximum emails to return (default 10, max 50)

        Returns:
            List of email summaries with: id, from, subject, snippet, date, is_unread
        """
        service = await self.get_service()
        if not service:
            return []

        max_results = min(max(1, max_results), 50)

        try:
            # Get inbox messages
            results = self._execute_with_backoff(
                service.users().messages().list(
                    userId='me',
                    q='in:inbox',
                    maxResults=max_results
                )
            )

            if not results:
                return []

            messages = results.get('messages', [])
            summaries = []

            for msg in messages:
                try:
                    msg_data = self._execute_with_backoff(
                        service.users().messages().get(
                            userId='me',
                            id=msg['id'],
                            format='metadata',
                            metadataHeaders=['From', 'Subject', 'Date']
                        )
                    )

                    if not msg_data:
                        continue

                    headers = {
                        h['name'].lower(): h['value']
                        for h in msg_data['payload'].get('headers', [])
                    }

                    label_ids = msg_data.get('labelIds', [])

                    summaries.append({
                        'id': msg_data['id'],
                        'from': headers.get('from', ''),
                        'subject': headers.get('subject', '(no subject)'),
                        'snippet': msg_data.get('snippet', ''),
                        'date': headers.get('date', ''),
                        'is_unread': 'UNREAD' in label_ids
                    })
                except HttpError as e:
                    logger.warning(f"Failed to fetch message {msg['id']}: {e}")
                    continue

            return summaries

        except HttpError as e:
            logger.error(f"Gmail inbox fetch failed: {e}")
            return []

    async def archive_email(self, message_id: str) -> bool:
        """
        Archive an email (remove from inbox).

        Args:
            message_id: Gmail message ID

        Returns:
            True if successful, False otherwise
        """
        service = await self.get_service()
        if not service:
            return False

        try:
            self._execute_with_backoff(
                service.users().messages().modify(
                    userId='me',
                    id=message_id,
                    body={'removeLabelIds': ['INBOX']}
                )
            )
            logger.info(f"Archived email {message_id}")
            return True
        except HttpError as e:
            logger.error(f"Failed to archive email {message_id}: {e}")
            return False

    async def trash_email(self, message_id: str) -> bool:
        """
        Move an email to trash.

        Args:
            message_id: Gmail message ID

        Returns:
            True if successful, False otherwise
        """
        service = await self.get_service()
        if not service:
            return False

        try:
            self._execute_with_backoff(
                service.users().messages().trash(
                    userId='me',
                    id=message_id
                )
            )
            logger.info(f"Trashed email {message_id}")
            return True
        except HttpError as e:
            logger.error(f"Failed to trash email {message_id}: {e}")
            return False

    async def mark_read(self, message_id: str) -> bool:
        """
        Mark an email as read.

        Args:
            message_id: Gmail message ID

        Returns:
            True if successful, False otherwise
        """
        service = await self.get_service()
        if not service:
            return False

        try:
            self._execute_with_backoff(
                service.users().messages().modify(
                    userId='me',
                    id=message_id,
                    body={'removeLabelIds': ['UNREAD']}
                )
            )
            logger.info(f"Marked email {message_id} as read")
            return True
        except HttpError as e:
            logger.error(f"Failed to mark email {message_id} as read: {e}")
            return False

    async def mark_unread(self, message_id: str) -> bool:
        """
        Mark an email as unread.

        Args:
            message_id: Gmail message ID

        Returns:
            True if successful, False otherwise
        """
        service = await self.get_service()
        if not service:
            return False

        try:
            self._execute_with_backoff(
                service.users().messages().modify(
                    userId='me',
                    id=message_id,
                    body={'addLabelIds': ['UNREAD']}
                )
            )
            logger.info(f"Marked email {message_id} as unread")
            return True
        except HttpError as e:
            logger.error(f"Failed to mark email {message_id} as unread: {e}")
            return False
