"""
In-memory TTL cache for external API responses.

Reduces redundant external API calls when the frontend polls endpoints
like email inbox and calendar agenda. Keeps things simple: no LRU eviction,
no max size — we're caching ~10 responses at most.

Usage:
    from web.core.cache import response_cache

    # Set with TTL
    response_cache.set("email_inbox_1", data, ttl_seconds=180)

    # Get (returns None if expired or missing)
    data = response_cache.get("email_inbox_1")

    # Invalidate on mutation
    response_cache.invalidate("email_inbox_1")
"""

import time
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ResponseCache:
    """Simple in-memory TTL cache for API responses."""

    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}
        self._hits: int = 0
        self._misses: int = 0

    def get(self, key: str) -> Optional[Any]:
        """
        Get a cached value by key.

        Returns None if key doesn't exist or has expired (lazy cleanup).
        """
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        if time.time() > entry["expires_at"]:
            # Expired — clean up and return None
            del self._store[key]
            self._misses += 1
            return None

        self._hits += 1
        return entry["data"]

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store a value with a TTL in seconds."""
        self._store[key] = {
            "data": value,
            "expires_at": time.time() + ttl_seconds,
            "ttl_seconds": ttl_seconds,
        }

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        if key in self._store:
            del self._store[key]
            logger.debug(f"Cache invalidated: {key}")

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove all keys that start with the given prefix."""
        keys_to_remove = [k for k in self._store if k.startswith(prefix)]
        for key in keys_to_remove:
            del self._store[key]
        if keys_to_remove:
            logger.debug(f"Cache invalidated {len(keys_to_remove)} keys with prefix: {prefix}")

    def clear(self) -> None:
        """Remove all cached entries."""
        self._store.clear()
        logger.debug("Cache cleared")

    def stats(self) -> dict:
        """
        Return cache statistics for observability.

        Returns hit/miss counts, current cached keys, and their TTLs.
        """
        now = time.time()
        cached_keys = {}
        for key, entry in self._store.items():
            remaining = entry["expires_at"] - now
            cached_keys[key] = {
                "ttl_seconds": entry["ttl_seconds"],
                "remaining_seconds": round(max(0, remaining), 1),
                "expired": remaining <= 0,
            }

        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, self._hits + self._misses) * 100, 1),
            "cached_keys_count": len(cached_keys),
            "cached_keys": cached_keys,
        }


# Module-level singleton
response_cache = ResponseCache()
