"""
Google Contacts API endpoints for Seny.

Provides endpoints for contact search and sync:
- POST /api/contacts/sync - Trigger contacts sync
- GET /api/contacts/status - Get sync status
- GET /api/contacts/search - Search contacts by name/email/phone
- GET /api/contacts/list - List all contacts
- GET /api/contacts/{resource_name} - Get contact details
- GET /api/contacts/stats - Get contact statistics
"""

from typing import Optional
import asyncio
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import list_gmail_tokens, get_db
from web.services.contacts_service import ContactsService, get_sync_progress, set_sync_progress


# Create contacts router
router = APIRouter()


# Response models
class SyncResponse(BaseModel):
    """Response for sync operation."""
    success: bool
    contacts_synced: int = 0
    errors: list[str] = []
    duration_seconds: float = 0
    message: str = ""


class SyncStatusResponse(BaseModel):
    """Response for sync status."""
    last_sync_at: Optional[str] = None
    contacts_synced: int = 0
    sync_in_progress: bool = False
    has_synced: bool = False


class AccountSyncStatus(BaseModel):
    """Sync status for a single account."""
    email: str
    contacts_synced: int = 0
    sync_in_progress: bool = False
    last_sync_at: Optional[str] = None


class AllAccountsSyncStatusResponse(BaseModel):
    """Response for aggregate sync status across all accounts."""
    total_contacts: int = 0
    any_sync_in_progress: bool = False
    all_syncs_complete: bool = True
    accounts: list[AccountSyncStatus] = []
    message: str = ""


class Contact(BaseModel):
    """A contact."""
    resource_name: str
    display_name: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    photo_url: Optional[str] = None


class ContactDetail(BaseModel):
    """Full contact details."""
    resource_name: str
    display_name: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    emails: list[dict] = []
    phones: list[dict] = []
    addresses: list[dict] = []
    company: Optional[str] = None
    job_title: Optional[str] = None
    notes: Optional[str] = None
    birthday: Optional[str] = None
    photo_url: Optional[str] = None


class SearchResponse(BaseModel):
    """Response for search endpoint."""
    contacts: list[Contact]
    query: str
    count: int


class ListResponse(BaseModel):
    """Response for list endpoint."""
    contacts: list[Contact]
    count: int


class StatsResponse(BaseModel):
    """Response for contact statistics."""
    total_contacts: int
    with_email: int
    with_phone: int


def _get_email(user_id: int, email: Optional[str]) -> str:
    """Get the email account to use - specified or first connected."""
    if email:
        return email
    accounts = list_gmail_tokens(user_id)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts connected. Connect Gmail first."
        )
    return accounts[0]["email"]


def _sync_contacts_blocking(user_id: int, email: str, full_sync: bool) -> dict:
    """
    Synchronous blocking function to run Contacts sync.
    This runs in a thread pool to avoid blocking the event loop.
    """
    import asyncio

    print(f"[CONTACTS SYNC THREAD] Starting blocking sync for {email}", flush=True)

    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        contacts = ContactsService(user_id, email)
        # Run the async sync in this thread's event loop
        result = loop.run_until_complete(contacts.sync_contacts(full_sync=full_sync))
        return result
    finally:
        loop.close()


async def _run_sync_in_background(user_id: int, email: str, full_sync: bool):
    """Run Contacts sync in background thread pool (non-blocking)."""
    print(f"[CONTACTS BACKGROUND SYNC] Starting sync for {email}", flush=True)

    # Mark sync as in_progress BEFORE spawning thread
    set_sync_progress(user_id, email, 0, True)

    try:
        result = await asyncio.to_thread(_sync_contacts_blocking, user_id, email, full_sync)

        if "error" in result:
            print(f"[CONTACTS BACKGROUND SYNC] Error for {email}: {result['error']}", flush=True)
        else:
            print(f"[CONTACTS BACKGROUND SYNC] Complete for {email}: {result.get('contacts_synced', 0)} contacts", flush=True)
    except Exception as e:
        print(f"[CONTACTS BACKGROUND SYNC] Exception for {email}: {e}", flush=True)
        set_sync_progress(user_id, email, 0, False)


async def _run_sync_all_accounts_in_background(user_id: int, full_sync: bool):
    """Run Contacts sync for ALL connected accounts in background."""
    accounts = list_gmail_tokens(user_id)
    print(f"[CONTACTS SYNC ALL] Starting sync for {len(accounts)} accounts", flush=True)

    # Mark ALL accounts as in_progress BEFORE starting
    for account in accounts:
        set_sync_progress(user_id, account["email"], 0, True)

    total_contacts = 0
    for account in accounts:
        email = account["email"]
        print(f"[CONTACTS SYNC ALL] Syncing account: {email}", flush=True)
        try:
            result = await asyncio.to_thread(_sync_contacts_blocking, user_id, email, full_sync)

            if "error" in result:
                print(f"[CONTACTS SYNC ALL] Error for {email}: {result['error']}", flush=True)
            else:
                contacts = result.get('contacts_synced', 0)
                total_contacts += contacts
                print(f"[CONTACTS SYNC ALL] Complete for {email}: {contacts} contacts", flush=True)
        except Exception as e:
            print(f"[CONTACTS SYNC ALL] Exception for {email}: {e}", flush=True)
            set_sync_progress(user_id, email, 0, False)

    print(f"[CONTACTS SYNC ALL] All accounts done: {total_contacts} total contacts", flush=True)


@router.post("/sync", response_model=SyncResponse)
async def sync_contacts(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account to sync (omit to sync ALL accounts)"),
    full_sync: bool = Query(False, description="Force full re-sync"),
    wait: bool = Query(False, description="Wait for sync to complete")
):
    """
    Trigger Google Contacts sync.

    Downloads contacts from Google People API and indexes locally for
    fast searching. Uses incremental sync when possible.

    Args:
        email: Google account to sync (optional, omit to sync ALL accounts)
        full_sync: Force full re-sync instead of incremental
        wait: Wait for sync to complete instead of running in background

    Returns:
        Sync results or acknowledgment that sync started
    """
    print(f"[CONTACTS SYNC] Request: user={user_id}, email={email}, full_sync={full_sync}, wait={wait}", flush=True)

    # If specific email provided, sync only that account
    if email:
        contacts = ContactsService(int(user_id), email)
        if not contacts.is_connected():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google account {email} is not connected"
            )

        if wait:
            result = await contacts.sync_contacts(full_sync=full_sync)
            if "error" in result:
                return SyncResponse(success=False, message=result["error"])
            return SyncResponse(
                success=True,
                contacts_synced=result.get("contacts_synced", 0),
                errors=result.get("errors", []),
                duration_seconds=result.get("duration_seconds", 0),
                message=f"Synced {result.get('contacts_synced', 0)} contacts"
            )
        else:
            asyncio.create_task(_run_sync_in_background(int(user_id), email, full_sync))
            return SyncResponse(
                success=True,
                message=f"Sync started for {email}. Check /api/contacts/status/all for progress."
            )

    # No email specified - sync ALL accounts
    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google accounts connected. Connect Gmail first."
        )

    asyncio.create_task(_run_sync_all_accounts_in_background(int(user_id), full_sync))

    account_list = ", ".join([a["email"] for a in accounts])
    return SyncResponse(
        success=True,
        message=f"Sync started for {len(accounts)} accounts: {account_list}. Check /api/contacts/status/all for progress."
    )


@router.get("/status", response_model=SyncStatusResponse)
async def get_sync_status_endpoint(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get Contacts sync status.

    Returns when the last sync occurred and how many contacts are indexed.
    """
    account_email = _get_email(int(user_id), email)

    contacts = ContactsService(int(user_id), account_email)
    if not contacts.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    status_data = await contacts.get_sync_status()

    return SyncStatusResponse(
        last_sync_at=status_data.get("last_sync_at"),
        contacts_synced=status_data.get("contacts_synced", 0),
        sync_in_progress=status_data.get("sync_in_progress", False),
        has_synced=status_data.get("has_synced", False)
    )


@router.get("/status/all", response_model=AllAccountsSyncStatusResponse)
async def get_all_accounts_sync_status(
    user_id: str = Depends(require_auth)
):
    """
    Get Contacts sync status for ALL connected accounts.
    """
    accounts = list_gmail_tokens(int(user_id))
    if not accounts:
        return AllAccountsSyncStatusResponse(
            message="No Google accounts connected"
        )

    account_statuses = []
    total_contacts = 0
    any_in_progress = False

    for account in accounts:
        email = account["email"]
        progress = get_sync_progress(int(user_id), email)
        contacts_count = progress.get("contacts_synced", 0)
        in_progress = progress.get("in_progress", False)

        total_contacts += contacts_count
        if in_progress:
            any_in_progress = True

        account_statuses.append(AccountSyncStatus(
            email=email,
            contacts_synced=contacts_count,
            sync_in_progress=in_progress,
            last_sync_at=None
        ))

    if any_in_progress:
        syncing = [a.email for a in account_statuses if a.sync_in_progress]
        message = f"Syncing: {', '.join(syncing)}... ({total_contacts} contacts so far)"
    else:
        message = f"Sync complete: {total_contacts} total contacts across {len(accounts)} accounts"

    return AllAccountsSyncStatusResponse(
        total_contacts=total_contacts,
        any_sync_in_progress=any_in_progress,
        all_syncs_complete=not any_in_progress,
        accounts=account_statuses,
        message=message
    )


@router.get("/search", response_model=SearchResponse)
async def search_contacts(
    q: str = Query(..., description="Search query"),
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    limit: int = Query(20, ge=1, le=100, description="Max results")
):
    """
    Search contacts by name, email, or phone.

    Uses full-text search to find contacts matching the query.
    Requires contacts to be synced first.

    Args:
        q: Search query (name, email, or phone)
        email: Google account (optional, defaults to first connected)
        limit: Maximum results (1-100, default 20)

    Returns:
        List of matching contacts
    """
    account_email = _get_email(int(user_id), email)

    contacts_svc = ContactsService(int(user_id), account_email)
    if not contacts_svc.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    results = await contacts_svc.search_contacts(q, limit=limit)

    return SearchResponse(
        contacts=[Contact(**c) for c in results],
        query=q,
        count=len(results)
    )


@router.get("/list", response_model=ListResponse)
async def list_contacts(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account"),
    limit: int = Query(100, ge=1, le=500, description="Max results")
):
    """
    List all contacts sorted by name.

    Args:
        email: Google account (optional, defaults to first connected)
        limit: Maximum results (1-500, default 100)

    Returns:
        List of contacts
    """
    account_email = _get_email(int(user_id), email)

    contacts_svc = ContactsService(int(user_id), account_email)
    if not contacts_svc.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    results = await contacts_svc.list_contacts(limit=limit)

    return ListResponse(
        contacts=[Contact(**c) for c in results],
        count=len(results)
    )


@router.get("/stats", response_model=StatsResponse)
async def get_contacts_stats(
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get contact statistics.

    Returns total count and counts with email/phone.
    """
    account_email = _get_email(int(user_id), email)

    contacts_svc = ContactsService(int(user_id), account_email)
    if not contacts_svc.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    stats = await contacts_svc.get_stats()

    return StatsResponse(
        total_contacts=stats.get("total_contacts", 0),
        with_email=stats.get("with_email", 0),
        with_phone=stats.get("with_phone", 0)
    )


@router.get("/{resource_name:path}", response_model=ContactDetail)
async def get_contact(
    resource_name: str,
    user_id: str = Depends(require_auth),
    email: Optional[str] = Query(None, description="Google account")
):
    """
    Get full contact details by resource name.

    Args:
        resource_name: Google resource name (e.g., "people/c123456")
        email: Google account (optional, defaults to first connected)

    Returns:
        Full contact details
    """
    account_email = _get_email(int(user_id), email)

    contacts_svc = ContactsService(int(user_id), account_email)
    if not contacts_svc.is_connected():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google account {account_email} is not connected"
        )

    contact = await contacts_svc.get_contact(resource_name)

    if not contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {resource_name} not found"
        )

    return ContactDetail(**contact)
