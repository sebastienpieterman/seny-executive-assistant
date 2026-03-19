"""
Embeddings API - Phase 23 (Vector Embeddings)

Provides status endpoint for monitoring ChromaDB embedding state.
"""
from fastapi import APIRouter, Depends
from web.auth.jwt_utils import require_auth
from web.core.database import get_embedding_tracking_summary
from web.services.embedding_service import EmbeddingService

router = APIRouter()


@router.get("/status")
async def get_embedding_status(user_id: int = Depends(require_auth)):
    """
    Returns embedding status for the authenticated user.

    Shows counts from both SQLite tracking table and ChromaDB collections.
    tracking_db reflects records written; chroma_collections reflects what's queryable.
    """
    svc = EmbeddingService()
    tracking = get_embedding_tracking_summary(user_id)

    collection_counts = {}
    if svc.enabled:
        for entity_type in EmbeddingService.COLLECTIONS:
            collection_counts[entity_type] = svc.get_collection_count(entity_type, user_id)

    return {
        "enabled": svc.enabled,
        "tracking_db": tracking,
        "chroma_collections": collection_counts,
    }
