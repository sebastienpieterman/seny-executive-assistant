"""
Second Brain Management API endpoints for Seny.

Provides a unified view across all Second Brain categories (People, Projects, Ideas).
- GET /api/second-brain/stats - Category counts
- GET /api/second-brain/items - List all items across categories
- GET /api/second-brain/items/{category}/{item_id} - Get item detail
- PUT /api/second-brain/items/{category}/{item_id} - Update item
- DELETE /api/second-brain/items/{category}/{item_id} - Delete item
- POST /api/second-brain/items/{category} - Create new item
- POST /api/second-brain/items/{category}/{item_id}/reclassify - Move to different category
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.database import (
    get_db,
    get_people_by_user, get_person, update_person, delete_person, search_people,
    get_projects_by_user, get_project, update_project, delete_project, search_projects,
    get_ideas_by_user, get_idea, update_idea, delete_idea, search_ideas,
    create_person, create_project, create_idea,
    get_person_followups, get_recent_inbox,
    merge_people, merge_ideas,
    find_duplicate_people, find_duplicate_ideas,
    dismiss_duplicate_pair,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Pydantic models ---

class SecondBrainItem(BaseModel):
    """Unified item representation for list view."""
    id: int
    category: str
    name: str
    subtitle: str = ""
    created_at: str = ""
    updated_at: str = ""
    confidence: Optional[float] = None
    original_text: Optional[str] = None


class SecondBrainListResponse(BaseModel):
    """Paginated list response."""
    items: list[SecondBrainItem]
    total: int


class StatsResponse(BaseModel):
    """Category counts."""
    people: int
    projects: int
    ideas: int
    total: int


class ItemUpdate(BaseModel):
    """Generic update model - optional fields for all categories."""
    name: Optional[str] = None
    context: Optional[str] = None
    notes: Optional[str] = None
    next_action: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[str] = None
    status: Optional[str] = None
    relationship_type: Optional[str] = None


class ReclassifyRequest(BaseModel):
    """Request to move item to a different category."""
    target_category: str
    reason: Optional[str] = None


class CreateItemRequest(BaseModel):
    """Request to create a new Second Brain item."""
    name: str  # Required for all categories
    context: Optional[str] = None  # People
    notes: Optional[str] = None  # All categories
    next_action: Optional[str] = None  # Projects
    status: Optional[str] = None  # Projects
    summary: Optional[str] = None  # Ideas
    tags: Optional[str] = None  # Ideas
    relationship_type: Optional[str] = None  # People


# --- Helper functions ---

def _get_inbox_info_for_item(user_id: int, table: str, item_id: int) -> dict:
    """Look up inbox_log entry for an item to get confidence and original_text."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT confidence, original_text
                FROM inbox_log
                WHERE user_id = %s AND routed_to_table = %s AND routed_to_id = %s
                ORDER BY created_at DESC LIMIT 1
            """, (user_id, table, item_id))
            row = cursor.fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {}


def _person_to_item(p: dict, inbox: dict = None) -> SecondBrainItem:
    return SecondBrainItem(
        id=p["id"],
        category="people",
        name=p.get("name") or "",
        subtitle=p.get("context") or "",
        created_at=p.get("created_at") or "",
        updated_at=p.get("updated_at") or "",
        confidence=inbox.get("confidence") if inbox else None,
        original_text=inbox.get("original_text") if inbox else None,
    )


def _project_to_item(p: dict, inbox: dict = None) -> SecondBrainItem:
    return SecondBrainItem(
        id=p["id"],
        category="projects",
        name=p.get("name") or "",
        subtitle=p.get("status") or "",
        created_at=p.get("created_at") or "",
        updated_at=p.get("updated_at") or "",
        confidence=inbox.get("confidence") if inbox else None,
        original_text=inbox.get("original_text") if inbox else None,
    )


def _idea_to_item(p: dict, inbox: dict = None) -> SecondBrainItem:
    return SecondBrainItem(
        id=p["id"],
        category="ideas",
        name=p.get("title") or "",
        subtitle=p.get("summary") or "",
        created_at=p.get("created_at") or "",
        updated_at=p.get("updated_at") or "",
        confidence=inbox.get("confidence") if inbox else None,
        original_text=inbox.get("original_text") if inbox else None,
    )


# --- Endpoints ---

@router.get("/stats", response_model=StatsResponse)
async def get_stats(user_id: str = Depends(require_auth)):
    """Get item counts per category."""
    uid = int(user_id)
    people = get_people_by_user(uid)
    projects = get_projects_by_user(uid)
    ideas = get_ideas_by_user(uid)
    return StatsResponse(
        people=len(people),
        projects=len(projects),
        ideas=len(ideas),
        total=len(people) + len(projects) + len(ideas),
    )


@router.get("/items", response_model=SecondBrainListResponse)
async def list_items(
    user_id: str = Depends(require_auth),
    category: Optional[str] = Query(None, description="Filter by category: people, projects, ideas"),
    search: Optional[str] = Query(None, description="Search query"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all Second Brain items, optionally filtered by category or search."""
    uid = int(user_id)
    all_items: list[SecondBrainItem] = []

    categories = [category] if category else ["people", "projects", "ideas"]

    for cat in categories:
        try:
            if cat == "people":
                if search:
                    rows = search_people(uid, search)
                else:
                    rows = get_people_by_user(uid)
                for r in rows:
                    inbox = _get_inbox_info_for_item(uid, "people", r["id"])
                    all_items.append(_person_to_item(r, inbox))

            elif cat == "projects":
                if search:
                    rows = search_projects(uid, search)
                else:
                    rows = get_projects_by_user(uid)
                for r in rows:
                    inbox = _get_inbox_info_for_item(uid, "projects", r["id"])
                    all_items.append(_project_to_item(r, inbox))

            elif cat == "ideas":
                if search:
                    rows = search_ideas(uid, search)
                else:
                    rows = get_ideas_by_user(uid)
                for r in rows:
                    inbox = _get_inbox_info_for_item(uid, "ideas", r["id"])
                    all_items.append(_idea_to_item(r, inbox))

        except Exception as e:
            import traceback
            print(f"Error loading {cat} items: {e}")
            traceback.print_exc()
            continue

    # Sort by created_at descending
    all_items.sort(key=lambda x: x.created_at or "", reverse=True)

    total = len(all_items)
    paginated = all_items[offset:offset + limit]

    return SecondBrainListResponse(items=paginated, total=total)


@router.get("/items/{category}/{item_id}")
async def get_item_detail(
    category: str,
    item_id: int,
    user_id: str = Depends(require_auth),
):
    """Get full detail for a specific item."""
    uid = int(user_id)
    item = None

    if category == "people":
        item = get_person(item_id)
        if item:
            item["followups"] = get_person_followups(item_id)
    elif category == "projects":
        item = get_project(item_id)
    elif category == "ideas":
        item = get_idea(item_id)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Verify ownership
    if item.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Item not found")

    # Add inbox info
    table_name = category
    inbox = _get_inbox_info_for_item(uid, table_name, item_id)
    item["confidence"] = inbox.get("confidence")
    item["original_text"] = inbox.get("original_text")
    item["category"] = category

    return item


@router.put("/items/{category}/{item_id}")
async def update_item(
    category: str,
    item_id: int,
    update: ItemUpdate,
    user_id: str = Depends(require_auth),
):
    """Update a Second Brain item."""
    uid = int(user_id)

    # Verify item exists and user owns it
    item = None
    if category == "people":
        item = get_person(item_id)
    elif category == "projects":
        item = get_project(item_id)
    elif category == "ideas":
        item = get_idea(item_id)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")

    if not item or item.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Item not found")

    # Build update fields based on category
    fields = {}
    if category == "people":
        if update.name is not None:
            fields["name"] = update.name
        if update.context is not None:
            fields["context"] = update.context
        if update.notes is not None:
            fields["notes"] = update.notes
        if update.relationship_type is not None:
            fields["relationship_type"] = update.relationship_type
        if fields:
            update_person(item_id, **fields)

    elif category == "projects":
        if update.name is not None:
            fields["name"] = update.name
        if update.next_action is not None:
            fields["next_action"] = update.next_action
        if update.notes is not None:
            fields["notes"] = update.notes
        if update.status is not None:
            fields["status"] = update.status
        if fields:
            update_project(item_id, **fields)

    elif category == "ideas":
        if update.name is not None:
            fields["title"] = update.name
        if update.summary is not None:
            fields["summary"] = update.summary
        if update.notes is not None:
            fields["notes"] = update.notes
        if update.tags is not None:
            fields["tags"] = update.tags
        if fields:
            update_idea(item_id, **fields)

    return {"status": "updated", "category": category, "id": item_id}


@router.delete("/items/{category}/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    category: str,
    item_id: int,
    user_id: str = Depends(require_auth),
):
    """Delete a Second Brain item and its inbox log entry."""
    uid = int(user_id)

    # Verify ownership
    item = None
    if category == "people":
        item = get_person(item_id)
    elif category == "projects":
        item = get_project(item_id)
    elif category == "ideas":
        item = get_idea(item_id)
    else:
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")

    if not item or item.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Item not found")

    # Delete from category table
    if category == "people":
        delete_person(item_id)
    elif category == "projects":
        delete_project(item_id)
    elif category == "ideas":
        delete_idea(item_id)

    # Also clean up inbox_log
    table_name = category
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM inbox_log WHERE routed_to_table = %s AND routed_to_id = %s",
                (table_name, item_id)
            )
    except Exception:
        pass

    return None


@router.post("/items/{category}", status_code=status.HTTP_201_CREATED)
async def create_item(
    category: str,
    request: CreateItemRequest,
    user_id: str = Depends(require_auth),
):
    """Create a new Second Brain item."""
    uid = int(user_id)
    
    if category not in ("people", "projects", "ideas"):
        raise HTTPException(status_code=400, detail=f"Invalid category: {category}")

    if not request.name or not request.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")

    new_id = None

    if category == "people":
        new_id = create_person(
            user_id=uid,
            name=request.name.strip(),
            context=request.context,
            notes=request.notes,
            relationship_type=request.relationship_type,
        )
    elif category == "projects":
        new_id = create_project(
            user_id=uid,
            name=request.name.strip(),
            next_action=request.next_action,
            notes=request.notes,
        )
        if request.status:
            update_project(new_id, status=request.status)
    elif category == "ideas":
        new_id = create_idea(
            user_id=uid,
            title=request.name.strip(),
            summary=request.summary,
            notes=request.notes,
            tags=request.tags,
        )
    
    if not new_id:
        raise HTTPException(status_code=500, detail="Failed to create item")
    
    return {"status": "created", "category": category, "id": new_id}


@router.post("/items/{category}/{item_id}/reclassify")
async def reclassify_item(
    category: str,
    item_id: int,
    request: ReclassifyRequest,
    user_id: str = Depends(require_auth),
):
    """Move an item from one category to another."""
    uid = int(user_id)
    target = request.target_category

    if target not in ("people", "projects", "ideas"):
        raise HTTPException(status_code=400, detail=f"Invalid target category: {target}")

    if target == category:
        raise HTTPException(status_code=400, detail="Item is already in that category")

    # Get existing item
    old_data = {}
    if category == "people":
        old_data = get_person(item_id) or {}
    elif category == "projects":
        old_data = get_project(item_id) or {}
    elif category == "ideas":
        old_data = get_idea(item_id) or {}
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source category: {category}")

    if not old_data or old_data.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Item not found")

    # Create in new category
    new_id = None
    new_table = None
    name = old_data.get("name") or old_data.get("title") or "Untitled"

    if target == "people":
        new_table = "people"
        new_id = create_person(
            user_id=uid,
            name=name,
            context=old_data.get("context") or old_data.get("summary"),
            notes=old_data.get("notes"),
        )
    elif target == "projects":
        new_table = "projects"
        new_id = create_project(
            user_id=uid,
            name=name,
            next_action=old_data.get("next_action"),
            notes=old_data.get("notes") or old_data.get("summary"),
        )
    elif target == "ideas":
        new_table = "ideas"
        new_id = create_idea(
            user_id=uid,
            title=name,
            summary=old_data.get("summary") or old_data.get("context"),
            notes=old_data.get("notes"),
            tags=old_data.get("tags"),
        )

    # Delete from old category
    if category == "people":
        delete_person(item_id)
    elif category == "projects":
        delete_project(item_id)
    elif category == "ideas":
        delete_idea(item_id)

    # Update inbox_log
    old_table = category
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE inbox_log
                SET classification = %s, routed_to_table = %s, routed_to_id = %s
                WHERE routed_to_table = %s AND routed_to_id = %s
            """, (target, new_table, new_id, old_table, item_id))
    except Exception:
        pass

    return {
        "status": "reclassified",
        "from_category": category,
        "to_category": target,
        "old_id": item_id,
        "new_id": new_id,
    }


def _enrich_captures_with_names(entries: list[dict]) -> list[dict]:
    """
    For each inbox_log entry that was routed to a table, look up the saved item's
    name/title so the UI can show it and link to it.

    Table name field mapping:
      people       → name
      projects     → name
      ideas        → title
    """
    # Group entry indices by table so we do one query per table
    by_table: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        table = e.get("routed_to_table")
        if table and e.get("routed_to_id") is not None:
            by_table.setdefault(table, []).append(i)

    name_col = {"people": "name", "projects": "name", "ideas": "title"}
    # frontend category slug used for click-through navigation
    category_slug = {"people": "people", "projects": "projects", "ideas": "ideas"}

    with get_db() as conn:
        cursor = conn.cursor()
        for table, indices in by_table.items():
            col = name_col.get(table)
            if not col:
                continue
            ids = [entries[i]["routed_to_id"] for i in indices]
            placeholders = ",".join("%s" * len(ids))
            try:
                cursor.execute(
                    f"SELECT id, {col} AS item_name FROM {table} WHERE id IN ({placeholders})",
                    ids,
                )
                name_map = {row["id"]: row["item_name"] for row in cursor.fetchall()}
            except Exception:
                name_map = {}
            slug = category_slug.get(table, table)
            for i in indices:
                item_id = entries[i]["routed_to_id"]
                entries[i]["item_name"] = name_map.get(item_id)
                entries[i]["item_category"] = slug

    return entries


@router.get("/captures")
async def get_captures(
    user_id: int = Depends(require_auth),
    limit: int = Query(100, ge=1, le=500),
    include_ignored: bool = Query(False),
):
    """
    Return inbox_log entries for the Captures tab.
    By default returns only entries that were routed to a table (i.e. something was saved).
    Pass include_ignored=true to also include 'none' classifications.
    Each routed entry is enriched with item_name and item_category for click-through.
    """
    entries = get_recent_inbox(user_id, limit=limit if include_ignored else 500)
    if not include_ignored:
        entries = [e for e in entries if e.get("routed_to_table")]
        entries = entries[:limit]
    entries = _enrich_captures_with_names(entries)
    return {"captures": entries, "count": len(entries)}


@router.delete("/captures/{capture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capture(
    capture_id: int,
    user_id: int = Depends(require_auth),
):
    """Delete an inbox_log entry (does not delete the routed item, only the log entry)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM inbox_log WHERE id = %s AND user_id = %s",
            (capture_id, user_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Capture log entry not found")


# ============================================================================
# Merge & Duplicate Endpoints (Phase 78)
# ============================================================================


class MergeRequest(BaseModel):
    category: str
    winner_id: int
    loser_id: int


@router.post("/merge")
async def merge_items(
    req: MergeRequest,
    user_id: int = Depends(require_auth),
):
    """Merge two records. Winner absorbs loser's data and references."""
    uid = int(user_id)

    if req.winner_id == req.loser_id:
        raise HTTPException(status_code=400, detail="Cannot merge an item with itself")

    if req.category == "people":
        result = merge_people(uid, req.winner_id, req.loser_id)
    elif req.category == "ideas":
        result = merge_ideas(uid, req.winner_id, req.loser_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported category: {req.category}")

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Merge failed"))

    return result


@router.get("/duplicates")
async def get_duplicates(
    category: str = Query(None, description="Filter by category: people or ideas"),
    user_id: int = Depends(require_auth),
):
    """Find duplicate records using name/title similarity scanning."""
    uid = int(user_id)
    result = {}

    if category is None or category == "people":
        result["people"] = find_duplicate_people(uid)
    if category is None or category == "ideas":
        result["ideas"] = find_duplicate_ideas(uid)

    return result


class DismissRequest(BaseModel):
    category: str
    ids: list[int]


@router.post("/duplicates/dismiss")
async def dismiss_duplicates(
    req: DismissRequest,
    user_id: int = Depends(require_auth),
):
    """Dismiss a group of items as not duplicates. All pairs in the group are dismissed."""
    uid = int(user_id)

    if len(req.ids) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 IDs to dismiss")

    # Dismiss all pairs in the group
    dismissed_count = 0
    for i in range(len(req.ids)):
        for j in range(i + 1, len(req.ids)):
            dismiss_duplicate_pair(uid, req.category, req.ids[i], req.ids[j])
            dismissed_count += 1

    return {"success": True, "pairs_dismissed": dismissed_count}
