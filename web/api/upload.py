"""
Upload endpoint for Seny — Phase 28 File Upload & Document Intelligence

POST /api/upload — accepts a multipart file, detects type, extracts content,
and returns a structured response for Claude to process.

POST /api/upload/save — routes extracted document text to note or ChromaDB embed.
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.document_extraction_service import DocumentExtractionService
from web.services.embedding_service import get_embedding_service
from web.services.notes_service import NotesService

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    file_name: str
    file_type: str
    text: Optional[str]
    image_b64: Optional[str]
    media_type: Optional[str]
    size_info: str
    truncated: bool
    truncation_notice: Optional[str]
    needs_storage_prompt: bool


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    user_id: int = Depends(require_auth),
):
    """
    Accept a file upload (multipart/form-data), extract its content, and
    return structured data for Claude to process.

    - Maximum file size: 25 MB
    - Supported types: PDF, Word (.docx), PowerPoint (.pptx), CSV, TXT, MD,
      HTML, and images (PNG, JPG, JPEG, WEBP, GIF)
    - Images are returned as base64 for Claude multimodal passthrough
    - Text documents are extracted and optionally truncated
    """
    content = await file.read()

    # Enforce 25 MB size limit
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large — maximum 25 MB",
        )

    logger.info(
        f"upload_file: user_id={user_id} filename={file.filename!r} "
        f"content_type={file.content_type!r} size={len(content)} bytes"
    )

    try:
        service = DocumentExtractionService()
        result = await service.extract(
            filename=file.filename or "",
            content_bytes=content,
            content_type=file.content_type or "",
        )
        return result
    except Exception as e:
        logger.error(f"upload_file: extraction error for user_id={user_id}: {repr(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"File processing failed: {repr(e)}",
        )


# ---------------------------------------------------------------------------
# Save request model
# ---------------------------------------------------------------------------


class SaveRequest(BaseModel):
    text: str
    title: str           # filename used as default note title
    mode: str            # "note" | "silent"
    tags: list[str] = []
    truncated: bool = False


# ---------------------------------------------------------------------------
# Save endpoint
# ---------------------------------------------------------------------------


@router.post("/save")
async def save_uploaded_document(
    request: SaveRequest,
    user_id: int = Depends(require_auth),
):
    """
    Save extracted document content to a note or ChromaDB (silent memory).

    - mode="note": creates a visible note via NotesService
    - mode="silent": embeds directly into ChromaDB without creating a note
    """
    if request.mode not in ("note", "silent"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'note' or 'silent'",
        )

    if request.truncated:
        logger.warning(
            f"save_uploaded_document: user_id={user_id} title={request.title!r} "
            f"is truncated — only the first portion will be saved"
        )

    if request.mode == "note":
        notes = NotesService(user_id)
        result = await notes.create_note(
            title=request.title,
            content=request.text,
            tags=request.tags,
        )
        response: dict = {"saved": True, "mode": "note", "note_id": result["id"]}
    else:
        # mode == "silent"
        doc_id = f"upload-{user_id}-{int(time.time())}"
        get_embedding_service().upsert("notes", [
            {
                "id": doc_id,
                "text": request.text,
                "metadata": {
                    "user_id": user_id,
                    "source": "upload",
                    "title": request.title,
                    "type": "uploaded_document",
                },
            }
        ])
        response = {"saved": True, "mode": "silent"}

    if request.truncated:
        response["warning"] = "Only the first portion was saved (file was truncated)."

    return response
