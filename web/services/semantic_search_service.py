"""
SemanticSearchService — Phase 24 Semantic Search

Wraps EmbeddingService to search across multiple ChromaDB collections,
merge and rank results by similarity, and filter by distance threshold.
"""
from __future__ import annotations

import logging

from web.services.embedding_service import EmbeddingService, get_embedding_service

logger = logging.getLogger(__name__)


class SemanticSearchService:
    """
    Multi-collection semantic search over ChromaDB.

    Searches one or more of the 6 entity type collections, merges results,
    sorts by distance ascending (most similar first), and filters out results
    above the distance threshold. Returns top n_results across all queried collections.

    If EmbeddingService is disabled (no VOYAGE_API_KEY), all methods return [].
    """

    ALL_ENTITY_TYPES = list(EmbeddingService.COLLECTIONS.keys())
    # ["items", "notes", "conversations", "people", "projects", "ideas"]

    def __init__(self):
        self.embedding_service = get_embedding_service()

    def search(
        self,
        user_id: int,
        query: str,
        entity_types: list | None = None,
        n_results: int = 10,
        threshold: float = 1.5,
    ) -> list:
        """
        Search for semantically similar content across ChromaDB collections.

        Args:
            user_id: Authenticated user's ID.
            query: Natural language search query.
            entity_types: Subset of ALL_ENTITY_TYPES to search. None = all 6 collections.
            n_results: Max results to return across all collections (default 10).
            threshold: Max L2 distance to include (ChromaDB default metric).
                       Default 1.5 is permissive; use 1.2-1.3 for tighter relevance.
                       L2 and cosine relate as: cosine_similarity = 1 - dist²/2

        Returns:
            List of result dicts sorted by distance ascending (most similar first):
            [{"entity_type": str, "id": str, "text": str, "metadata": dict,
              "distance": float, "similarity": float}]
            Returns [] when embeddings are disabled or query is empty.
        """
        if not self.embedding_service.enabled:
            return []

        if not query or not query.strip():
            return []

        target_types = entity_types if entity_types is not None else self.ALL_ENTITY_TYPES

        # Drop unknown entity types silently (don't crash on bad input)
        valid_types = [et for et in target_types if et in EmbeddingService.COLLECTIONS]
        if not valid_types:
            return []

        # Fetch more candidates per collection than the final n_results so that
        # threshold filtering across merged collections still leaves enough results.
        per_collection_limit = max(n_results * 2, 20)

        all_results = []
        for entity_type in valid_types:
            try:
                results = self.embedding_service.query_similar(
                    entity_type, user_id, query, n_results=per_collection_limit
                )
                for r in results:
                    dist = r.get("distance")
                    if dist is None or dist > threshold:
                        continue
                    all_results.append({
                        "entity_type": entity_type,
                        "id": r["id"],
                        "text": r["text"],
                        "metadata": r.get("metadata", {}),
                        "distance": round(dist, 4),
                        "similarity": round(max(0.0, 1 - dist ** 2 / 2), 4),
                    })
            except Exception as e:
                logger.error(f"SemanticSearchService.search({entity_type}, user_id={user_id}): {repr(e)}")

        # Sort by distance ascending (most similar first), return top n
        all_results.sort(key=lambda x: x["distance"])
        return all_results[:n_results]
