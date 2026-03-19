"""
Telegram MTProto client service for Seny.

Uses Telethon library to access personal Telegram chats via MTProto protocol.
Unlike bot tokens, this uses user authentication to access personal messages.

SECURITY:
- Session strings are equivalent to being logged in - store securely
- Never log session strings
- Rate limit operations to avoid Telegram bans

CONNECTION POOLING:
- TelegramClientPool maintains persistent connections to avoid rate limiting
- Each (user_id, phone_number) pair gets one persistent client
- Clients are reused across requests instead of creating new ones
- Prevents HTTP 429 errors from too many connection attempts
"""

import os
import asyncio
from typing import Optional
from datetime import datetime

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError
from telethon.tl.types import User, Chat, Channel

from web.core.database import (
    save_telegram_session,
    get_telegram_session,
    list_telegram_sessions,
    delete_telegram_session,
    get_first_telegram_session,
    update_telegram_last_active
)


# =============================================================================
# Connection Pool Singleton
# =============================================================================

class TelegramClientPool:
    """
    Singleton pool for managing persistent Telegram client connections.

    Instead of creating a new TelegramClient for every request (which causes
    rate limiting), this pool maintains persistent connections that are reused.

    Usage:
        pool = TelegramClientPool.get_instance()
        client = await pool.get_client(user_id, phone_number)
        # Use client for operations...
        # Do NOT disconnect - client stays in pool
    """

    _instance: Optional['TelegramClientPool'] = None
    _lock = asyncio.Lock()

    def __init__(self):
        """Initialize the pool. Use get_instance() instead of calling directly."""
        # Pool: {(user_id, phone_number): TelegramClient}
        self._clients: dict[tuple[int, str], TelegramClient] = {}
        # Connection locks to prevent race conditions during connect
        self._connect_locks: dict[tuple[int, str], asyncio.Lock] = {}
        # API credentials (same for all clients)
        self.api_id = int(os.getenv('TELEGRAM_API_ID', 0))
        self.api_hash = os.getenv('TELEGRAM_API_HASH', '')

    @classmethod
    def get_instance(cls) -> 'TelegramClientPool':
        """Get the singleton instance of the pool."""
        if cls._instance is None:
            cls._instance = TelegramClientPool()
        return cls._instance

    def _get_lock(self, key: tuple[int, str]) -> asyncio.Lock:
        """Get or create a lock for a specific client key."""
        if key not in self._connect_locks:
            self._connect_locks[key] = asyncio.Lock()
        return self._connect_locks[key]

    async def get_client(
        self,
        user_id: int,
        phone_number: str,
        session_string: str = None
    ) -> Optional[TelegramClient]:
        """
        Get a connected TelegramClient from the pool.

        If a client exists and is connected, returns it immediately.
        If no client exists or it's disconnected, creates/reconnects it.

        Args:
            user_id: Seny user ID
            phone_number: Telegram phone number
            session_string: Optional session string (if not provided, loads from DB)

        Returns:
            Connected TelegramClient or None if connection fails
        """
        if not self.api_id or not self.api_hash:
            print("[DEBUG] TelegramClientPool: API credentials not configured")
            return None

        key = (user_id, phone_number)
        lock = self._get_lock(key)

        async with lock:
            # Check if we have an existing connected client
            if key in self._clients:
                client = self._clients[key]

                # Verify it's still connected
                if client.is_connected():
                    try:
                        # Quick authorization check
                        if await client.is_user_authorized():
                            print(f"[DEBUG] TelegramClientPool: Reusing existing client for user {user_id}")
                            update_telegram_last_active(user_id, phone_number)
                            return client
                    except Exception as e:
                        print(f"[DEBUG] TelegramClientPool: Existing client auth check failed: {e}")

                # Client exists but disconnected or unauthorized - remove it
                print(f"[DEBUG] TelegramClientPool: Removing stale client for user {user_id}")
                try:
                    await client.disconnect()
                except:
                    pass
                del self._clients[key]

            # Need to create a new client
            print(f"[DEBUG] TelegramClientPool: Creating new client for user {user_id}")

            # Get session string if not provided
            if not session_string:
                session_data = get_telegram_session(user_id, phone_number)
                if not session_data:
                    print(f"[DEBUG] TelegramClientPool: No session found for user {user_id}, phone {phone_number}")
                    return None
                session_string = session_data['session_string']

            # Create and connect new client
            try:
                client = TelegramClient(
                    StringSession(session_string),
                    self.api_id,
                    self.api_hash
                )

                await client.connect()

                if not await client.is_user_authorized():
                    print(f"[DEBUG] TelegramClientPool: Client not authorized for user {user_id}")
                    await client.disconnect()
                    return None

                # Store in pool
                self._clients[key] = client
                update_telegram_last_active(user_id, phone_number)

                print(f"[DEBUG] TelegramClientPool: Successfully connected client for user {user_id}")
                return client

            except Exception as e:
                print(f"[DEBUG] TelegramClientPool: Failed to connect client: {e}")
                return None

    async def remove_client(self, user_id: int, phone_number: str) -> None:
        """
        Remove and disconnect a client from the pool.

        Call this when a user disconnects their Telegram account.
        """
        key = (user_id, phone_number)
        lock = self._get_lock(key)

        async with lock:
            if key in self._clients:
                print(f"[DEBUG] TelegramClientPool: Removing client for user {user_id}")
                client = self._clients[key]
                try:
                    await client.disconnect()
                except:
                    pass
                del self._clients[key]

    def get_pool_stats(self) -> dict:
        """Get statistics about the connection pool (for debugging)."""
        return {
            'active_clients': len(self._clients),
            'client_keys': list(self._clients.keys())
        }


# Global pool instance accessor
def get_telegram_pool() -> TelegramClientPool:
    """Get the global TelegramClientPool instance."""
    return TelegramClientPool.get_instance()


class TelegramService:
    """
    Service for Telegram integration using MTProto client.

    Handles authentication, chat listing, message retrieval, and sending.
    Each instance is tied to a specific user_id.
    """

    def __init__(self, user_id: int, phone_number: str = None):
        """
        Initialize TelegramService for a user.

        Args:
            user_id: Seny user ID
            phone_number: Optional specific phone to use (if None, uses first connected)
        """
        self.user_id = user_id
        self.phone_number = phone_number
        self.api_id = int(os.getenv('TELEGRAM_API_ID', 0))
        self.api_hash = os.getenv('TELEGRAM_API_HASH', '')
        self.client: Optional[TelegramClient] = None
        self._pending_phone_code_hash = None

    def is_configured(self) -> bool:
        """Check if Telegram API credentials are configured."""
        return bool(self.api_id and self.api_hash)

    def is_connected(self) -> bool:
        """Check if user has any connected Telegram accounts."""
        sessions = list_telegram_sessions(self.user_id)
        return len(sessions) > 0

    def list_accounts(self) -> list[dict]:
        """List all connected Telegram accounts for this user."""
        return list_telegram_sessions(self.user_id)

    # =========================================================================
    # Authentication Flow
    # =========================================================================

    async def start_auth(self, phone_number: str) -> dict:
        """
        Start authentication flow - requests SMS code from Telegram.

        Args:
            phone_number: Phone number with country code (e.g., "+1234567890")

        Returns:
            Dictionary with phone_code_hash for completing auth
        """
        if not self.is_configured():
            return {"error": "Telegram API credentials not configured"}

        # Create new client with empty session
        self.client = TelegramClient(
            StringSession(),
            self.api_id,
            self.api_hash
        )

        await self.client.connect()

        # Request verification code
        sent = await self.client.send_code_request(phone_number)

        # Store for later use in complete_auth
        self._pending_phone_code_hash = sent.phone_code_hash
        self.phone_number = phone_number

        return {
            "phone_code_hash": sent.phone_code_hash,
            "phone_number": phone_number
        }

    async def complete_auth(
        self,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        password: str = None
    ) -> dict:
        """
        Complete authentication with SMS code (and optional 2FA password).

        Args:
            phone_number: Phone number being authenticated
            code: SMS verification code
            phone_code_hash: Hash from start_auth
            password: 2FA password if enabled

        Returns:
            Dictionary with user info on success, error on failure
        """
        if not self.client:
            return {"error": "No pending authentication. Call start_auth first."}

        try:
            # Try to sign in with code
            await self.client.sign_in(
                phone_number,
                code,
                phone_code_hash=phone_code_hash
            )
        except SessionPasswordNeededError:
            # 2FA is enabled - need password
            if not password:
                return {
                    "error": "2FA enabled",
                    "requires_2fa": True,
                    "phone_code_hash": phone_code_hash
                }

            # Sign in with password
            await self.client.sign_in(password=password)
        except PhoneCodeExpiredError:
            return {"error": "Verification code expired. Please request a new code."}
        except Exception as e:
            return {"error": f"Authentication failed: {str(e)}"}

        # Get user info
        me = await self.client.get_me()

        # Save session string to database
        session_string = self.client.session.save()
        display_name = f"{me.first_name} {me.last_name or ''}".strip()

        save_telegram_session(
            user_id=self.user_id,
            phone_number=phone_number,
            session_string=session_string,
            user_name=me.username,
            display_name=display_name
        )

        return {
            "success": True,
            "username": me.username,
            "display_name": display_name,
            "phone_number": phone_number
        }

    async def connect(self) -> bool:
        """
        Connect to Telegram using stored session (via connection pool).

        Uses TelegramClientPool to reuse existing connections instead of
        creating new ones for each request, which prevents rate limiting.

        Returns:
            True if connected and authorized, False otherwise
        """
        if not self.is_configured():
            return False

        # Determine phone number if not specified
        if not self.phone_number:
            session_data = get_first_telegram_session(self.user_id)
            if not session_data:
                return False
            self.phone_number = session_data['phone_number']

        # Get client from pool (creates or reuses existing)
        pool = get_telegram_pool()
        self.client = await pool.get_client(self.user_id, self.phone_number)

        return self.client is not None

    async def disconnect(self, remove_session: bool = False) -> None:
        """
        Disconnect from Telegram.

        With connection pooling, this method behaves differently:
        - If remove_session=False: Just clears local reference (client stays in pool)
        - If remove_session=True: Removes from pool AND deletes session from database

        Args:
            remove_session: If True, fully disconnect and remove session from database
        """
        if remove_session and self.phone_number:
            # Actually remove from pool and delete session
            pool = get_telegram_pool()
            await pool.remove_client(self.user_id, self.phone_number)
            delete_telegram_session(self.user_id, self.phone_number)

        # Clear local reference (but client stays alive in pool)
        self.client = None

    # =========================================================================
    # Chat Operations
    # =========================================================================

    async def list_dialogs(self, limit: int = 50) -> list[dict]:
        """
        List all chats (DMs, groups, channels).

        Args:
            limit: Maximum number of dialogs to return

        Returns:
            List of dialog dictionaries
        """
        if not self.client:
            if not await self.connect():
                return []

        dialogs = await self.client.get_dialogs(limit=limit)

        result = []
        for d in dialogs:
            result.append({
                'id': d.id,
                'name': d.name,
                'type': self._get_dialog_type(d),
                'unread_count': d.unread_count,
                'last_message': d.message.text if d.message else None,
                'last_message_date': d.message.date.isoformat() if d.message else None
            })

        return result

    async def get_messages(
        self,
        chat_id: int,
        limit: int = 20,
        offset_id: int = 0
    ) -> list[dict]:
        """
        Get messages from a chat.

        Args:
            chat_id: Chat ID to get messages from
            limit: Maximum messages to return
            offset_id: Message ID to start from (for pagination)

        Returns:
            List of message dictionaries (newest first)
        """
        if not self.client:
            if not await self.connect():
                return []

        messages = await self.client.get_messages(
            chat_id,
            limit=limit,
            offset_id=offset_id
        )

        result = []
        for m in messages:
            if not m.text and not m.message:
                continue  # Skip media-only messages for now

            sender_name = await self._get_sender_name(m)

            result.append({
                'id': m.id,
                'text': m.text or m.message,
                'sender': sender_name,
                'sender_id': m.sender_id,
                'date': m.date.isoformat(),
                'is_outgoing': m.out,
                'reply_to_msg_id': m.reply_to_msg_id if hasattr(m, 'reply_to_msg_id') else None
            })

        return result

    async def send_message(self, chat_id: int, text: str, reply_to: int = None) -> dict:
        """
        Send a message to a chat.

        Args:
            chat_id: Chat ID to send to
            text: Message text
            reply_to: Message ID to reply to (optional)

        Returns:
            Dictionary with message info
        """
        if not self.client:
            if not await self.connect():
                return {"error": "Not connected to Telegram"}

        msg = await self.client.send_message(chat_id, text, reply_to=reply_to)

        return {
            'id': msg.id,
            'text': msg.text,
            'date': msg.date.isoformat(),
            'chat_id': chat_id
        }

    async def send_gif(self, chat_id: int, gif_url: str, reply_to: int = None) -> dict:
        """
        Send a GIF to a chat.

        Args:
            chat_id: Chat ID to send to
            gif_url: URL of the GIF to send
            reply_to: Message ID to reply to (optional)

        Returns:
            Dictionary with message info
        """
        if not self.client:
            if not await self.connect():
                return {"error": "Not connected to Telegram"}

        try:
            # Telethon can send files directly from URLs
            msg = await self.client.send_file(
                chat_id,
                gif_url,
                reply_to=reply_to,
                force_document=False  # Let Telegram determine the best format
            )

            return {
                'id': msg.id,
                'date': msg.date.isoformat(),
                'chat_id': chat_id
            }
        except Exception as e:
            return {"error": f"Failed to send GIF: {str(e)}"}

    async def send_reaction(self, chat_id: int, message_id: int, emoji: str) -> dict:
        """
        Send a reaction to a message.

        Args:
            chat_id: Chat ID containing the message
            message_id: Message ID to react to
            emoji: Emoji to react with (e.g., "👍", "❤️", "😂")

        Returns:
            Dictionary with success status
        """
        if not self.client:
            if not await self.connect():
                return {"error": "Not connected to Telegram"}

        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji

            # Send the reaction
            await self.client(SendReactionRequest(
                peer=chat_id,
                msg_id=message_id,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))

            return {
                'success': True,
                'chat_id': chat_id,
                'message_id': message_id,
                'emoji': emoji
            }
        except Exception as e:
            print(f"[DEBUG] send_reaction error: {e}")
            return {"error": f"Failed to send reaction: {str(e)}"}

    async def mark_as_read(self, chat_id: int) -> dict:
        """Mark all messages in a chat as read using Telethon's send_read_acknowledge.

        Called when user opens a chat in the UI to clear unread badges on Telegram's servers.
        """
        if not self.client:
            if not await self.connect():
                return {"error": "Not connected to Telegram"}

        try:
            entity = await self.client.get_entity(chat_id)
            await self.client.send_read_acknowledge(entity)
            return {"success": True, "chat_id": chat_id}
        except Exception as e:
            print(f"[DEBUG] mark_as_read error: {e}")
            return {"success": False, "error": str(e)}

    async def search_messages(
        self,
        query: str,
        limit: int = 20,
        chat_id: int = None
    ) -> list[dict]:
        """
        Search messages globally or in a specific chat.

        Args:
            query: Search query
            limit: Maximum results to return
            chat_id: Optional chat ID to search in (None = global search)

        Returns:
            List of matching message dictionaries
        """
        if not self.client:
            if not await self.connect():
                return []

        if chat_id:
            # Search within specific chat
            messages = await self.client.get_messages(
                chat_id,
                limit=limit,
                search=query
            )
        else:
            # Global search across all chats
            from telethon.tl.functions.messages import SearchGlobalRequest
            from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

            result = await self.client(SearchGlobalRequest(
                q=query,
                filter=InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=0,
                limit=limit
            ))

            messages = result.messages

        results = []
        for m in messages:
            if not hasattr(m, 'message') or not m.message:
                continue

            # Get chat name for global search results
            chat_name = None
            if hasattr(m, 'peer_id'):
                try:
                    entity = await self.client.get_entity(m.peer_id)
                    if hasattr(entity, 'title'):
                        chat_name = entity.title
                    elif hasattr(entity, 'first_name'):
                        chat_name = f"{entity.first_name} {entity.last_name or ''}".strip()
                except:
                    pass

            results.append({
                'id': m.id,
                'text': m.message,
                'date': m.date.isoformat() if hasattr(m, 'date') else None,
                'chat_name': chat_name,
                'chat_id': m.peer_id if hasattr(m, 'peer_id') else None
            })

        # Sort by date, newest first (None dates go to end)
        results.sort(key=lambda x: x.get('date') or '', reverse=True)

        return results

    async def get_chat_info(self, chat_id: int) -> Optional[dict]:
        """
        Get information about a chat.

        Args:
            chat_id: Chat ID

        Returns:
            Chat info dictionary or None
        """
        if not self.client:
            if not await self.connect():
                return None

        try:
            entity = await self.client.get_entity(chat_id)

            info = {
                'id': chat_id,
                'type': 'unknown'
            }

            if isinstance(entity, User):
                info['type'] = 'user'
                info['name'] = f"{entity.first_name} {entity.last_name or ''}".strip()
                info['username'] = entity.username
                info['phone'] = entity.phone
            elif isinstance(entity, Chat):
                info['type'] = 'group'
                info['name'] = entity.title
                info['participants_count'] = entity.participants_count
            elif isinstance(entity, Channel):
                info['type'] = 'channel' if entity.broadcast else 'supergroup'
                info['name'] = entity.title
                info['username'] = entity.username

            return info
        except Exception as e:
            print(f"[DEBUG] get_chat_info error: {e}")
            return None

    async def resolve_chat(self, identifier: str) -> Optional[int]:
        """
        Resolve a chat identifier to a chat ID.

        Handles:
        - Numeric IDs (returned as-is)
        - Usernames (@username)
        - Display names (searched in dialogs)

        Args:
            identifier: Chat identifier (ID, username, or display name)

        Returns:
            Chat ID if found, None otherwise
        """
        if not self.client:
            if not await self.connect():
                return None

        # If it's already a numeric ID, return it
        try:
            return int(identifier)
        except ValueError:
            pass

        # Remove @ prefix if present
        clean_identifier = identifier.lstrip('@')

        try:
            # Try to resolve by username first
            entity = await self.client.get_entity(clean_identifier)
            return entity.id
        except Exception:
            pass

        # Fall back to searching dialogs by name
        try:
            dialogs = await self.client.get_dialogs(limit=100)
            identifier_lower = clean_identifier.lower()

            for dialog in dialogs:
                # Check display name
                if dialog.name and dialog.name.lower() == identifier_lower:
                    return dialog.id

                # Check username for users
                entity = dialog.entity
                if hasattr(entity, 'username') and entity.username:
                    if entity.username.lower() == identifier_lower:
                        return dialog.id

                # Check first_name for users
                if hasattr(entity, 'first_name') and entity.first_name:
                    if entity.first_name.lower() == identifier_lower:
                        return dialog.id
                    # Also try full name
                    full_name = f"{entity.first_name} {entity.last_name or ''}".strip().lower()
                    if full_name == identifier_lower:
                        return dialog.id

            # If no exact match, try partial match
            for dialog in dialogs:
                if dialog.name and identifier_lower in dialog.name.lower():
                    return dialog.id

        except Exception as e:
            print(f"[DEBUG] resolve_chat error searching dialogs: {e}")

        return None

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_dialog_type(self, dialog) -> str:
        """Determine the type of a dialog."""
        entity = dialog.entity

        if isinstance(entity, User):
            if entity.bot:
                return 'bot'
            return 'dm'
        elif isinstance(entity, Chat):
            return 'group'
        elif isinstance(entity, Channel):
            if entity.broadcast:
                return 'channel'
            return 'supergroup'

        return 'unknown'

    async def _get_sender_name(self, message) -> str:
        """Get the display name of a message sender."""
        try:
            sender = await message.get_sender()

            if sender is None:
                return "Unknown"

            if hasattr(sender, 'first_name'):
                return f"{sender.first_name} {sender.last_name or ''}".strip()
            elif hasattr(sender, 'title'):
                return sender.title

            return str(sender.id)
        except Exception:
            return "Unknown"
