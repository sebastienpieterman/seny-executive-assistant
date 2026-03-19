"""
Search API — Phase 24 (Semantic Search)

Provides semantic search endpoint backed by ChromaDB vector embeddings.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.semantic_search_service import SemanticSearchService

router = APIRouter()


class SemanticSearchRequest(BaseModel):
    query: str
    entity_types: Optional[List[str]] = None  # None = search all 6 collections
    n_results: int = 10
    threshold: float = 1.5


@router.post("/semantic")
async def semantic_search(
    body: SemanticSearchRequest,
    user_id: int = Depends(require_auth),
):
    """
    POST /api/search/semantic

    Returns semantically similar content across all or specified ChromaDB collections.
    Results are sorted by similarity (most similar first).

    Response shape:
    {
        "results": [{"entity_type", "id", "text", "metadata", "distance", "similarity"}, ...],
        "query": str,
        "n_results_found": int,
        "embeddings_enabled": bool
    }

    If embeddings are not enabled (VOYAGE_API_KEY not set), returns empty results
    with embeddings_enabled: false — does not raise an error.
    """
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    svc = SemanticSearchService()

    if not svc.embedding_service.enabled:
        return {
            "results": [],
            "query": body.query,
            "n_results_found": 0,
            "embeddings_enabled": False,
        }

    results = svc.search(
        user_id=user_id,
        query=body.query,
        entity_types=body.entity_types,
        n_results=body.n_results,
        threshold=body.threshold,
    )

    return {
        "results": results,
        "query": body.query,
        "n_results_found": len(results),
        "embeddings_enabled": True,
    }
