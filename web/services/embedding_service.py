"""
EmbeddingService — Phase 23 Vector Embeddings Foundation

Manages ChromaDB collections and Voyage AI text embeddings.
User-agnostic: no user_id in constructor. user_id goes in document metadata.
Gracefully degrades when VOYAGE_API_KEY is not set.
"""
import hashlib
import logging
import os

logger = logging.getLogger(__name__)


_embedding_service_instance = None


def get_embedding_service():
    """Return the process-wide singleton EmbeddingService instance.

    ChromaDB PersistentClient must not be opened multiple times in the same
    process pointing at the same path — doing so causes the HNSW index to
    appear empty to the second client even though count() shows documents.
    All callers (migration, search, scheduler) share one instance.
    """
    global _embedding_service_instance
    if _embedding_service_instance is None:
        _embedding_service_instance = EmbeddingService()
    return _embedding_service_instance


class EmbeddingService:
    """
    Core vector embedding service backed by ChromaDB and Voyage AI voyage-4-lite.

    Collection names are class constants. The service is user-agnostic —
    user_id is stored in ChromaDB document metadata and used as a filter on queries.

    If VOYAGE_API_KEY is not set, self.enabled = False and all methods return
    early with safe defaults (no crashes, no exceptions propagated).
    """

    COLLECTIONS = {
        "items": "seny_items",
        "notes": "seny_notes",
        "conversations": "seny_conversations",
        "people": "seny_people",
        "projects": "seny_projects",
        "ideas": "seny_ideas",
    }

    def __init__(self):
        api_key = os.getenv("VOYAGE_API_KEY")
        if not api_key:
            logger.warning(
                "EmbeddingService: VOYAGE_API_KEY not set — embedding disabled. "
                "Set VOYAGE_API_KEY to enable vector embeddings."
            )
            self.enabled = False
            return

        self.enabled = True

        try:
            import chromadb
            chroma_path = os.getenv("CHROMA_PATH", "/data/chroma")
            self.chroma = chromadb.PersistentClient(path=chroma_path)
            logger.info(f"EmbeddingService: ChromaDB PersistentClient initialized at {chroma_path}")
        except Exception as e:
            logger.error(f"EmbeddingService: Failed to initialize ChromaDB: {repr(e)}")
            self.enabled = False
            return

        try:
            import voyageai
            self.voyage_client = voyageai.Client(api_key=api_key)
            logger.info("EmbeddingService: Voyage AI client initialized (model: voyage-4-lite)")
        except Exception as e:
            logger.error(f"EmbeddingService: Failed to initialize Voyage AI client: {repr(e)}")
            self.enabled = False

    def _get_collection(self, entity_type: str):
        """
        Returns or creates a ChromaDB collection for the given entity type.
        No custom embedding function — embeddings are provided explicitly via
        the embeddings= parameter on upsert (avoids needing sentence-transformers).
        """
        collection_name = self.COLLECTIONS[entity_type]
        return self.chroma.get_or_create_collection(name=collection_name)

    def _embed_texts(self, texts: list, input_type: str = "document") -> list:
        """
        Calls Voyage AI embeddings API in a single batch and returns embedding vectors.
        Returns [] for empty input. Caller is responsible for batching (max 100 per call).
        input_type: "document" for storing content, "query" for search queries.
        """
        if not texts:
            return []
        result = self.voyage_client.embed(texts, model="voyage-4-lite", input_type=input_type)
        return result.embeddings

    def upsert(self, entity_type: str, docs: list) -> int:
        """
        Embed and upsert documents into the ChromaDB collection for entity_type.

        Each doc must have: {"id": str, "user_id": int, "text": str, "metadata": dict}

        Filters out docs with empty/None text. Returns count of docs upserted.
        Returns 0 on any error (logs error, does not raise).
        """
        if not self.enabled:
            return 0

        try:
            # Filter out docs with empty or None text
            valid_docs = [d for d in docs if d.get("text")]
            if not valid_docs:
                return 0

            texts = [d["text"] for d in valid_docs]
            embeddings = self._embed_texts(texts)

            collection = self._get_collection(entity_type)
            collection.upsert(
                ids=[d["id"] for d in valid_docs],
                embeddings=embeddings,
                documents=texts,
                metadatas=[{**d["metadata"], "user_id": d["user_id"]} for d in valid_docs],
            )
            return len(valid_docs)
        except Exception as e:
            logger.error(f"EmbeddingService.upsert({entity_type}): {repr(e)}")
            return 0

    def query_similar(self, entity_type: str, user_id: int, query_text: str, n_results: int = 10) -> list:
        """
        Find semantically similar documents for the given user and entity type.

        Returns list of dicts: [{"id": str, "text": str, "metadata": dict, "distance": float}]
        Returns [] when disabled or on error.

        Full usage happens in Phase 24 (semantic search). Implementation is complete here.
        """
        if not self.enabled:
            return []

        try:
            query_embeddings = self._embed_texts([query_text], input_type="query")
            if not query_embeddings:
                return []

            collection = self._get_collection(entity_type)
            total_count = collection.count()
            # Fetch more candidates than requested since we post-filter by user_id.
            # ChromaDB 0.6+ where filter in query() is unreliable; filter in Python instead.
            fetch_n = min(n_results * 10, total_count)
            if fetch_n == 0:
                return []
            results = collection.query(
                query_embeddings=query_embeddings,
                n_results=fetch_n,
                include=["documents", "metadatas", "distances"],
            )

            output = []
            ids = results.get("ids", [[]])[0]
            documents = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            for i, doc_id in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                # Filter by user_id in Python (ChromaDB 0.6+ where filter unreliable in query())
                if meta.get("user_id") != int(user_id):
                    continue
                output.append({
                    "id": doc_id,
                    "text": documents[i] if i < len(documents) else "",
                    "metadata": meta,
                    "distance": distances[i] if i < len(distances) else None,
                })
                if len(output) >= n_results:
                    break
            return output
        except Exception as e:
            logger.error(f"EmbeddingService.query_similar({entity_type}, user_id={user_id}): {repr(e)}")
            return []

    def get_collection_count(self, entity_type: str, user_id: int) -> int:
        """
        Returns count of embedded documents for this user in the given collection.
        Returns 0 on any error.
        """
        if not self.enabled:
            return 0

        try:
            collection = self._get_collection(entity_type)
            result = collection.get(include=["metadatas"])
            user_ids = [m.get("user_id") for m in (result.get("metadatas") or [])]
            return sum(1 for uid in user_ids if uid == int(user_id))
        except Exception as e:
            logger.error(f"EmbeddingService.get_collection_count({entity_type}, user_id={user_id}): {repr(e)}")
            return 0

    @staticmethod
    def _hash(text: str) -> str:
        """Returns MD5 hex digest of text for content change detection."""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    async def _embed_and_track(self, entity_type: str, user_id: int, docs: list) -> int:
        """
        Embed a list of docs and record them in embedding_tracking for idempotency.

        Filters out docs with empty text, skips already-embedded IDs, batches in
        groups of 100, calls upsert(), then saves tracking records.

        Args:
            entity_type: One of the COLLECTIONS keys
            user_id: User whose data is being embedded
            docs: List of dicts with keys: id, text, content_hash, metadata

        Returns:
            Count of docs actually embedded
        """
        from web.core.database import get_unembedded_ids, save_embedding_records

        # Filter docs with empty or None text
        valid_docs = [d for d in docs if d.get("text")]
        if not valid_docs:
            return 0

        # Only embed IDs not yet tracked
        candidate_ids = [d["id"] for d in valid_docs]
        unembedded_ids = set(get_unembedded_ids(entity_type, user_id, candidate_ids))
        to_embed = [d for d in valid_docs if d["id"] in unembedded_ids]

        if not to_embed:
            return 0

        total_embedded = 0

        # Process in batches of 100
        for i in range(0, len(to_embed), 100):
            batch = to_embed[i:i + 100]

            # Build docs in the format upsert() expects
            upsert_docs = [
                {
                    "id": d["id"],
                    "user_id": user_id,
                    "text": d["text"],
                    "metadata": d.get("metadata", {}),
                }
                for d in batch
            ]

            count = self.upsert(entity_type, upsert_docs)

            if count > 0:
                # Save tracking records for successfully embedded docs
                records = [
                    {
                        "entity_type": entity_type,
                        "entity_id": d["id"],
                        "user_id": user_id,
                        "content_hash": d["content_hash"],
                    }
                    for d in batch
                ]
                save_embedding_records(records)
                total_embedded += count

        return total_embedded

    async def _embed_user_data(self, user_id: int) -> dict:
        """
        Embed all data for a single user across all 6 entity types.

        Shared implementation used by both run_embedding_migration() and
        embed_new_items(). Idempotent: only embeds items not yet in
        embedding_tracking for this user.

        Args:
            user_id: User whose data should be embedded

        Returns:
            Dict with counts per entity type and total:
            {"items": N, "notes": N, "conversations": N, "people": N, "projects": N, "ideas": N, "total": N}

        Raises:
            Exception: Propagates to caller (run_embedding_migration / embed_new_items wrap in try/except)
        """
        from web.core.database import (
            get_all_item_classifications,
            get_all_notes_for_embedding,
            get_all_conversations_for_embedding,
            get_all_people_for_embedding,
            get_all_projects_for_embedding,
            get_all_ideas_for_embedding,
        )

        # --- Items ---
        item_rows = get_all_item_classifications(user_id)
        item_docs = []
        for row in item_rows:
            summary = row.get("summary")
            if summary is None:
                continue
            thread_summary = row.get("thread_summary")
            text = summary + "\n" + thread_summary if thread_summary else summary
            content_hash = self._hash(text)
            item_docs.append({
                "id": f"item_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {
                    "source": row.get("source", ""),
                    "relevance": row.get("relevance", ""),
                    "urgency": row.get("urgency", ""),
                },
            })

        # --- Notes ---
        note_rows = get_all_notes_for_embedding(user_id)
        note_docs = []
        for row in note_rows:
            title = row.get("title") or ""
            content = row.get("content") or ""
            text = f"{title}\n{content}"
            content_hash = self._hash(text)
            note_docs.append({
                "id": f"note_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {"title": title},
            })

        # --- Conversations ---
        conv_rows = get_all_conversations_for_embedding(user_id)
        conv_docs = []
        for row in conv_rows:
            text = row.get("text") or ""
            if not text:
                continue
            text = text[:8000]
            content_hash = self._hash(text)
            conv_docs.append({
                "id": f"conv_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {},
            })

        # --- People ---
        people_rows = get_all_people_for_embedding(user_id)
        people_docs = []
        for row in people_rows:
            name = row.get("name") or ""
            context = row.get("context") or ""
            notes = row.get("notes") or ""
            text = "\n".join(filter(None, [name, context, notes]))
            # Skip if only name present (no additional context)
            if len(text) <= len(name):
                continue
            content_hash = self._hash(text)
            people_docs.append({
                "id": f"person_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {"name": name},
            })

        # --- Projects ---
        project_rows = get_all_projects_for_embedding(user_id)
        project_docs = []
        for row in project_rows:
            name = row.get("name") or ""
            status = row.get("status") or ""
            next_action = row.get("next_action") or ""
            notes = row.get("notes") or ""
            text = "\n".join(filter(None, [name, status, next_action, notes]))
            content_hash = self._hash(text)
            project_docs.append({
                "id": f"project_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {"name": name, "status": status},
            })

        # --- Ideas ---
        idea_rows = get_all_ideas_for_embedding(user_id)
        idea_docs = []
        for row in idea_rows:
            title = row.get("title") or ""
            summary = row.get("summary") or ""
            notes = row.get("notes") or ""
            tags = row.get("tags") or ""
            text = "\n".join(filter(None, [title, summary, notes, tags]))
            content_hash = self._hash(text)
            idea_docs.append({
                "id": f"idea_{row['id']}",
                "text": text,
                "content_hash": content_hash,
                "metadata": {"title": title},
            })

        # Embed each entity type
        items_count = await self._embed_and_track("items", user_id, item_docs)
        notes_count = await self._embed_and_track("notes", user_id, note_docs)
        conversations_count = await self._embed_and_track("conversations", user_id, conv_docs)
        people_count = await self._embed_and_track("people", user_id, people_docs)
        projects_count = await self._embed_and_track("projects", user_id, project_docs)
        ideas_count = await self._embed_and_track("ideas", user_id, idea_docs)

        total = items_count + notes_count + conversations_count + people_count + projects_count + ideas_count

        return {
            "items": items_count,
            "notes": notes_count,
            "conversations": conversations_count,
            "people": people_count,
            "projects": projects_count,
            "ideas": ideas_count,
            "total": total,
        }

    async def run_embedding_migration(self, user_id: int) -> dict:
        """
        Embed all historical data for a user across all 6 entity types.

        Fetches existing records from the database, constructs text representations,
        and embeds any that haven't been embedded yet (idempotent via content hash tracking).

        Args:
            user_id: User whose historical data should be embedded

        Returns:
            Dict with counts per entity type and total:
            {"items": N, "notes": N, "conversations": N, "people": N, "projects": N, "ideas": N, "total": N}
        """
        zeros = {"items": 0, "notes": 0, "conversations": 0, "people": 0, "projects": 0, "ideas": 0, "total": 0}

        if not self.enabled:
            return zeros

        try:
            return await self._embed_user_data(user_id)
        except Exception as e:
            logger.error(f"EmbeddingService.run_embedding_migration(user_id={user_id}): {repr(e)}")
            return zeros

    async def embed_new_items(self, user_id: int = None) -> dict:
        """
        Embed any new (untracked) items for one user or all users.

        Called periodically by the APScheduler job every 30 minutes to keep
        ChromaDB current as new items are created after the initial migration.

        Args:
            user_id: If provided, embed only for this user. If None, embed for all users.

        Returns:
            Dict with accumulated counts per entity type and total across all users:
            {"items": N, "notes": N, "conversations": N, "people": N, "projects": N, "ideas": N, "total": N}
        """
        zeros = {"items": 0, "notes": 0, "conversations": 0, "people": 0, "projects": 0, "ideas": 0, "total": 0}

        if not self.enabled:
            return zeros

        try:
            from web.core.database import get_db

            if user_id is not None:
                user_ids = [user_id]
            else:
                with get_db() as db:
                    cursor = db.cursor()
                    cursor.execute("SELECT id FROM users")
                    rows = cursor.fetchall()
                user_ids = [row[0] if isinstance(row, tuple) else row["id"] for row in rows]

            totals = {"items": 0, "notes": 0, "conversations": 0, "people": 0, "projects": 0, "ideas": 0, "total": 0}

            for uid in user_ids:
                try:
                    result = await self._embed_user_data(uid)
                    for key in totals:
                        totals[key] += result.get(key, 0)
                except Exception as e:
                    logger.error(f"EmbeddingService.embed_new_items(user_id={uid}): {repr(e)}")

            if totals["total"] > 0:
                logger.info(f"embed_new_items: {totals['total']} new items embedded across all users")

            return totals

        except Exception as e:
            logger.error(f"EmbeddingService.embed_new_items: {repr(e)}")
            return zeros
