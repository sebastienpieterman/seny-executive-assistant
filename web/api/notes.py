"""
Notes endpoints for Seny.

Notes management API for creating, reading, searching, and exporting notes:
- GET /api/notes - List recent notes
- GET /api/notes/search - Search notes (FTS5 or tag filter)
- GET /api/notes/tags - List all tags with counts
- GET /api/notes/by-tag/{tag} - Get notes by tag
- GET /api/notes/{id} - Get single note with links/backlinks
- POST /api/notes - Create note
- PUT /api/notes/{id} - Update note
- DELETE /api/notes/{id} - Delete note
- GET /api/notes/graph - Get graph data for visualization
- GET /api/notes/{id}/export - Export single note as markdown
- GET /api/notes/export-all - Export all notes as zip
"""

import io
import logging
import re
import zipfile
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query

logger = logging.getLogger(__name__)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.notes_service import NotesService


# Create notes router
router = APIRouter()


# Request/Response models
class NoteCreate(BaseModel):
    """Request model for creating a note."""
    title: str
    content: str


class NoteUpdate(BaseModel):
    """Request model for updating a note."""
    title: Optional[str] = None
    content: Optional[str] = None


class NoteSummary(BaseModel):
    """Note summary for list view."""
    id: int
    title: str
    content_preview: str
    tags: list[str]
    created_at: str
    updated_at: str


class LinkedNote(BaseModel):
    """Linked note reference."""
    id: int
    title: str


class NoteDetail(BaseModel):
    """Full note details."""
    id: int
    title: str
    content: str
    tags: list[str]
    linked_notes: list[LinkedNote]
    backlinks: list[LinkedNote]
    created_at: str
    updated_at: str


class NotesListResponse(BaseModel):
    """Response for list notes endpoint."""
    notes: list[NoteSummary]
    total: int


class TagInfo(BaseModel):
    """Tag with count."""
    tag: str
    count: int


class TagsResponse(BaseModel):
    """Response for list tags endpoint."""
    tags: list[TagInfo]


class GraphNode(BaseModel):
    """Node in graph visualization."""
    id: int
    title: str
    tags: list[str]
    size: int


class GraphEdge(BaseModel):
    """Edge in graph visualization."""
    source: int
    target: int
    type: str  # "link" or "tag"


class TagCluster(BaseModel):
    """Tag cluster for graph."""
    tag: str
    notes: list[int]


class GraphResponse(BaseModel):
    """Response for graph endpoint."""
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    tag_clusters: list[TagCluster]


def _note_to_summary(note: dict) -> NoteSummary:
    """Convert note dict to summary model."""
    content = note.get("content", "")
    # Create preview: first 100 chars, strip newlines
    preview = content[:100].replace("\n", " ").strip()
    if len(content) > 100:
        preview += "..."

    return NoteSummary(
        id=note["id"],
        title=note["title"],
        content_preview=preview,
        tags=note.get("tags", []),
        created_at=note["created_at"],
        updated_at=note["updated_at"]
    )


def _note_to_detail(note: dict) -> NoteDetail:
    """Convert note dict to detail model."""
    return NoteDetail(
        id=note["id"],
        title=note["title"],
        content=note["content"],
        tags=note.get("tags", []),
        linked_notes=[LinkedNote(id=ln["id"], title=ln["title"]) for ln in note.get("linked_notes", [])],
        backlinks=[LinkedNote(id=bl["id"], title=bl["title"]) for bl in note.get("backlinks", [])],
        created_at=note["created_at"],
        updated_at=note["updated_at"]
    )


def _sanitize_filename(title: str) -> str:
    """Sanitize title for use as filename."""
    # Replace spaces with hyphens
    filename = title.replace(" ", "-")
    # Remove special characters except hyphens and underscores
    filename = re.sub(r'[^\w\-]', '', filename)
    # Limit length
    if len(filename) > 100:
        filename = filename[:100]
    # Ensure not empty
    if not filename:
        filename = "untitled"
    return filename


def _note_to_markdown(note: dict) -> str:
    """Convert note to markdown with YAML frontmatter."""
    # Format tags without # prefix for YAML
    tags_yaml = ""
    if note.get("tags"):
        tags_list = "\n".join(f"  - {tag.lstrip('#')}" for tag in note["tags"])
        tags_yaml = f"tags:\n{tags_list}"

    # Build frontmatter
    frontmatter = f"""---
title: {note['title']}
created: {note['created_at']}
updated: {note['updated_at']}
{tags_yaml}
---"""

    # Build content
    content = note.get("content", "")

    # Add note title as H1 if not already present
    if not content.strip().startswith(f"# {note['title']}"):
        content = f"# {note['title']}\n\n{content}"

    return f"{frontmatter}\n\n{content}"


@router.get("", response_model=NotesListResponse)
async def list_notes(
    user_id: str = Depends(require_auth),
    limit: int = Query(20, ge=1, le=100, description="Max notes to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """
    List recent notes for the authenticated user.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of notes with summaries, most recent first
    """
    print(f"[DEBUG] list_notes API: user_id={user_id}, type={type(user_id)}, int(user_id)={int(user_id)}")
    notes_service = NotesService(int(user_id))
    notes = await notes_service.list_notes(limit=limit, offset=offset)
    print(f"[DEBUG] list_notes API: found {len(notes)} notes")

    return NotesListResponse(
        notes=[_note_to_summary(n) for n in notes],
        total=len(notes)
    )


@router.get("/search", response_model=NotesListResponse)
async def search_notes(
    user_id: str = Depends(require_auth),
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100, description="Max notes to return")
):
    """
    Search notes using full-text search.

    Protected endpoint - requires valid JWT token.

    Supports:
    - Free text search (FTS5)
    - Tag filter: "tag:work" or "tag:#work"

    Returns:
        List of matching notes
    """
    notes_service = NotesService(int(user_id))
    notes = await notes_service.search_notes(query=q, limit=limit)

    return NotesListResponse(
        notes=[_note_to_summary(n) for n in notes],
        total=len(notes)
    )


@router.get("/tags", response_model=TagsResponse)
async def list_tags(user_id: str = Depends(require_auth)):
    """
    List all tags with counts for the authenticated user.

    Protected endpoint - requires valid JWT token.

    Returns:
        List of tags with note counts, sorted by count descending
    """
    notes_service = NotesService(int(user_id))
    tags = await notes_service.list_all_tags()

    return TagsResponse(
        tags=[TagInfo(tag=t["tag"], count=t["count"]) for t in tags]
    )


@router.get("/by-tag/{tag}", response_model=NotesListResponse)
async def get_notes_by_tag(
    tag: str,
    user_id: str = Depends(require_auth),
    limit: int = Query(50, ge=1, le=100, description="Max notes to return")
):
    """
    Get all notes with a specific tag.

    Protected endpoint - requires valid JWT token.

    Args:
        tag: Tag name (with or without # prefix)

    Returns:
        List of notes with the specified tag
    """
    notes_service = NotesService(int(user_id))
    notes = await notes_service.get_notes_by_tag(tag=tag, limit=limit)

    return NotesListResponse(
        notes=[_note_to_summary(n) for n in notes],
        total=len(notes)
    )


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    user_id: str = Depends(require_auth),
    tag: Optional[str] = Query(None, description="Filter graph by tag")
):
    """
    Get graph data for visualization.

    Protected endpoint - requires valid JWT token.

    Returns nodes (notes) and edges (links between notes).
    Optionally filters by tag.

    Returns:
        Nodes, edges, and tag clusters for D3 visualization
    """
    notes_service = NotesService(int(user_id))
    graph_data = await notes_service.get_graph_data()

    # If filtering by tag, filter nodes and edges
    if tag:
        # Normalize tag (remove # prefix for comparison since tags are stored without #)
        tag_normalized = tag.lstrip("#").lower()

        # Get notes with this tag
        tag_note_ids = set()
        for cluster in graph_data.get("tag_clusters", []):
            if cluster["tag"] == tag_normalized:
                tag_note_ids = set(cluster.get("notes", []))
                break

        # Filter nodes
        filtered_nodes = [
            n for n in graph_data.get("nodes", [])
            if n["id"] in tag_note_ids
        ]

        # Filter edges to only include edges between filtered nodes
        filtered_edges = [
            e for e in graph_data.get("edges", [])
            if e["source"] in tag_note_ids and e["target"] in tag_note_ids
        ]

        # Filter tag clusters
        filtered_clusters = [
            {"tag": c["tag"], "notes": [nid for nid in c.get("notes", []) if nid in tag_note_ids]}
            for c in graph_data.get("tag_clusters", [])
            if any(nid in tag_note_ids for nid in c.get("notes", []))
        ]

        return GraphResponse(
            nodes=[GraphNode(
                id=n["id"],
                title=n["title"],
                tags=n["tags"],
                size=n.get("size", 10)
            ) for n in filtered_nodes],
            edges=[GraphEdge(
                source=e["source"],
                target=e["target"],
                type=e.get("type", "link")
            ) for e in filtered_edges],
            tag_clusters=[TagCluster(tag=c["tag"], notes=c["notes"]) for c in filtered_clusters]
        )

    # Return full graph
    return GraphResponse(
        nodes=[GraphNode(
            id=n["id"],
            title=n["title"],
            tags=n["tags"],
            size=n.get("size", 10)
        ) for n in graph_data.get("nodes", [])],
        edges=[GraphEdge(
            source=e["source"],
            target=e["target"],
            type=e.get("type", "link")
        ) for e in graph_data.get("edges", [])],
        tag_clusters=[TagCluster(
            tag=c["tag"],
            notes=c.get("notes", [])
        ) for c in graph_data.get("tag_clusters", [])]
    )


@router.get("/export-all")
async def export_all_notes(user_id: str = Depends(require_auth)):
    """
    Export all notes as a zip file of markdown files.

    Protected endpoint - requires valid JWT token.

    Returns:
        Zip file containing all notes as .md files
    """
    notes_service = NotesService(int(user_id))
    notes = await notes_service.list_notes(limit=1000, offset=0)

    if not notes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No notes to export"
        )

    # Create zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for note in notes:
            # Get full note with tags
            full_note = await notes_service.get_note(note["id"])
            if full_note:
                filename = f"{_sanitize_filename(full_note['title'])}.md"
                content = _note_to_markdown(full_note)
                zip_file.writestr(filename, content)

    zip_buffer.seek(0)

    # Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="seny-notes-{timestamp}.zip"'
        }
    )


@router.get("/{note_id}", response_model=NoteDetail)
async def get_note(
    note_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Get a specific note with full details.

    Protected endpoint - requires valid JWT token.

    Args:
        note_id: The note's ID

    Returns:
        Full note details including linked notes and backlinks
    """
    notes_service = NotesService(int(user_id))
    note = await notes_service.get_note(note_id)

    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found"
        )

    # Fetch linked notes and backlinks
    note["linked_notes"] = await notes_service.get_linked_notes(note_id)
    note["backlinks"] = await notes_service.get_backlinks(note_id)

    return _note_to_detail(note)


@router.get("/{note_id}/export")
async def export_note(
    note_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Export a single note as a markdown file.

    Protected endpoint - requires valid JWT token.

    Args:
        note_id: The note's ID

    Returns:
        Markdown file with YAML frontmatter
    """
    notes_service = NotesService(int(user_id))
    note = await notes_service.get_note(note_id)

    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found"
        )

    content = _note_to_markdown(note)
    filename = f"{_sanitize_filename(note['title'])}.md"

    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@router.post("", response_model=NoteDetail, status_code=status.HTTP_201_CREATED)
async def create_note(
    note: NoteCreate,
    user_id: str = Depends(require_auth)
):
    """
    Create a new note.

    Protected endpoint - requires valid JWT token.

    Tags are automatically extracted from content (e.g., #work, #project).
    Wiki-links are parsed (e.g., [[Other Note]]) and linked if target exists.

    Returns:
        The created note with full details
    """
    notes_service = NotesService(int(user_id))
    created_note = await notes_service.create_note(
        title=note.title,
        content=note.content
    )

    if not created_note:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create note"
        )

    return _note_to_detail(created_note)


@router.put("/{note_id}", response_model=NoteDetail)
async def update_note(
    note_id: int,
    note: NoteUpdate,
    user_id: str = Depends(require_auth)
):
    """
    Update an existing note.

    Protected endpoint - requires valid JWT token.

    Args:
        note_id: The note's ID
        note: Fields to update (title and/or content)

    Returns:
        The updated note with full details
    """
    notes_service = NotesService(int(user_id))

    # Check note exists
    existing = await notes_service.get_note(note_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found"
        )

    # Build update kwargs
    update_kwargs = {}
    if note.title is not None:
        update_kwargs["title"] = note.title
    if note.content is not None:
        update_kwargs["content"] = note.content

    if not update_kwargs:
        # Nothing to update, return existing
        return _note_to_detail(existing)

    updated_note = await notes_service.update_note(note_id, **update_kwargs)

    if not updated_note:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update note"
        )

    return _note_to_detail(updated_note)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: int,
    user_id: str = Depends(require_auth)
):
    """
    Delete a note.

    Protected endpoint - requires valid JWT token.

    Args:
        note_id: The note's ID

    Returns:
        204 No Content on success
    """
    notes_service = NotesService(int(user_id))

    # Check note exists
    existing = await notes_service.get_note(note_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found"
        )

    success = await notes_service.delete_note(note_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete note"
        )

    return None
