"""
Scanner Status API - Phase 13-05

Provides endpoints for monitoring scanner health, triggering manual scans,
and viewing entity resolution status.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.core.cache import response_cache
from web.core.database import get_db
from web.services.scanner_service import ScannerService, SOURCE_CONFIGS

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# Request/Response Models
# ============================================================================

class ScanRequest(BaseModel):
    """Request body for triggering a manual scan."""
    source: str  # Source name (e.g. "gmail") or "all"


class SourceStatus(BaseModel):
    """Status of a single scanner source."""
    source: str
    last_scan: Optional[str] = None
    status: str
    items_found: int = 0
    items_new: int = 0
    next_scan_due: Optional[str] = None
    # Drip-mode fields (only present for Slack; None for all other sources)
    channels_tracked: Optional[int] = None
    channels_excluded: Optional[int] = None
    channels_with_open_circuit: Optional[int] = None
    scanner_mode: Optional[str] = None


class EntityResolutionStatus(BaseModel):
    """Summary of entity resolution state."""
    last_run: Optional[str] = None
    mappings_total: int = 0
    unresolved_count: int = 0


class ScannerStatusResponse(BaseModel):
    """Response for GET /api/scanner/status."""
    sources: list[SourceStatus]
    entity_resolution: EntityResolutionStatus


class ScanResultItem(BaseModel):
    """Result of scanning a single source."""
    source: str
    status: str
    items_found: int = 0
    items_new: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


class ScanResponse(BaseModel):
    """Response for POST /api/scanner/scan."""
    scan_results: list[ScanResultItem]
    entity_resolution: Optional[dict] = None


class UnresolvedEntity(BaseModel):
    """An unresolved entity mapping."""
    source: str
    identifier: str
    display_name: Optional[str] = None


class EntitySummaryResponse(BaseModel):
    """Response for GET /api/scanner/entities."""
    total_mappings: int
    resolved: int
    unresolved: int
    by_source: dict
    recent_unresolved: list[UnresolvedEntity]


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/status", response_model=ScannerStatusResponse)
async def get_scanner_status(user_id: str = Depends(require_auth)):
    """
    Get scan status for all sources.

    Returns last scan time, status, items found, and calculated next scan due time
    for each configured source, plus entity resolution summary.
    """
    from datetime import datetime, timedelta

    uid = int(user_id)
    service = ScannerService(uid)
    source_status = await service.get_scan_status()

    _drip_status_map = {
        'active': 'completed',
        'circuit_open': 'failed',
        'never_run': 'never_run',
        'all_excluded': 'completed',
    }

    sources = []
    for source_name, config in SOURCE_CONFIGS.items():
        # Slack uses drip architecture — read from slack_channel_cursors, not scanner_runs
        if source_name == 'slack':
            from web.core.database import get_slack_drip_status
            drip = get_slack_drip_status(uid)
            sources.append(SourceStatus(
                source='slack',
                last_scan=drip['last_scan'],
                status=_drip_status_map.get(drip['status'], 'never_run'),
                items_found=drip['items_found_24h'],
                items_new=drip['items_found_24h'],
                channels_tracked=drip['channels_tracked'],
                channels_excluded=drip['channels_excluded'],
                channels_with_open_circuit=drip['channels_with_open_circuit'],
                scanner_mode='drip',
                next_scan_due='continuous',
            ))
            continue

        info = source_status.get(source_name, {})
        last_scan = info.get('last_scan')

        # Calculate next_scan_due from last_scan + interval
        next_due = None
        if last_scan:
            try:
                last_dt = datetime.fromisoformat(last_scan)
                interval = timedelta(minutes=config['interval_minutes'])
                next_dt = last_dt + interval
                next_due = next_dt.isoformat()
            except (ValueError, TypeError):
                pass

        sources.append(SourceStatus(
            source=source_name,
            last_scan=last_scan,
            status=info.get('status', 'never_run'),
            items_found=info.get('items_found', 0),
            items_new=info.get('items_new', 0),
            next_scan_due=next_due,
        ))

    # Entity resolution summary
    er_status = EntityResolutionStatus()
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Total mappings and unresolved count
            cursor.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN person_id IS NULL THEN 1 ELSE 0 END) as unresolved
                FROM entity_mappings WHERE user_id = %s
            """, (uid,))
            row = cursor.fetchone()
            if row:
                er_status.mappings_total = row['total'] or 0
                er_status.unresolved_count = row['unresolved'] or 0

            # Last entity resolution run (most recent scanner_run for any source)
            cursor.execute("""
                SELECT MAX(completed_at) as last_run
                FROM scanner_runs
                WHERE user_id = %s AND status = 'completed'
            """, (uid,))
            row = cursor.fetchone()
            if row and row['last_run']:
                er_status.last_run = row['last_run']
    except Exception as e:
        logger.error(f"Error fetching entity resolution status: {e}")

    return ScannerStatusResponse(sources=sources, entity_resolution=er_status)


@router.post("/scan")
async def trigger_scan(request: ScanRequest, background_tasks: BackgroundTasks, user_id: str = Depends(require_auth)):
    """
    Trigger a manual scan for one or all sources.

    For "all": starts in the background and returns immediately (scans can take
    several minutes and would otherwise time out on the HTTP layer).
    For a single source: runs synchronously and returns results.
    """
    uid = int(user_id)
    service = ScannerService(uid)
    source = request.source.lower().strip()

    if source == "all":
        background_tasks.add_task(service.run_all_scans, resolve_entities=True)
        return {"status": "started", "message": "Scan started in background"}
    elif source in SOURCE_CONFIGS:
        try:
            result = await service.run_scan(source)
            return ScanResponse(
                scan_results=[ScanResultItem(
                    source=result.get('source', source),
                    status=result.get('status', 'unknown'),
                    items_found=result.get('items_found', 0),
                    items_new=result.get('items_new', 0),
                    duration_seconds=result.get('duration_seconds', 0.0),
                    error=result.get('error'),
                )]
            )
        except Exception as e:
            return ScanResponse(
                scan_results=[ScanResultItem(
                    source=source,
                    status='failed',
                    error=str(e),
                )]
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown source: {source}. Valid sources: {', '.join(SOURCE_CONFIGS.keys())}, all"
        )


@router.get("/entities", response_model=EntitySummaryResponse)
async def get_entity_summary(user_id: str = Depends(require_auth)):
    """
    Get entity resolution summary.

    Returns total mappings, resolved/unresolved counts, breakdown by source,
    and recent unresolved entities for manual review.
    """
    uid = int(user_id)

    total = 0
    resolved = 0
    unresolved = 0
    by_source = {}
    recent_unresolved = []

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Overall counts
            cursor.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN person_id IS NOT NULL THEN 1 ELSE 0 END) as resolved,
                       SUM(CASE WHEN person_id IS NULL THEN 1 ELSE 0 END) as unresolved
                FROM entity_mappings WHERE user_id = %s
            """, (uid,))
            row = cursor.fetchone()
            if row:
                total = row['total'] or 0
                resolved = row['resolved'] or 0
                unresolved = row['unresolved'] or 0

            # By source
            cursor.execute("""
                SELECT source, COUNT(*) as count
                FROM entity_mappings WHERE user_id = %s
                GROUP BY source
            """, (uid,))
            for row in cursor.fetchall():
                by_source[row['source']] = row['count']

            # Recent unresolved (limit 20)
            cursor.execute("""
                SELECT source, source_identifier, display_name
                FROM entity_mappings
                WHERE user_id = %s AND person_id IS NULL
                ORDER BY updated_at DESC
                LIMIT 20
            """, (uid,))
            for row in cursor.fetchall():
                recent_unresolved.append(UnresolvedEntity(
                    source=row['source'],
                    identifier=row['source_identifier'],
                    display_name=row['display_name'],
                ))

    except Exception as e:
        logger.error(f"Error fetching entity summary: {e}")

    return EntitySummaryResponse(
        total_mappings=total,
        resolved=resolved,
        unresolved=unresolved,
        by_source=by_source,
        recent_unresolved=recent_unresolved,
    )


@router.post("/reset")
async def reset_stuck_scans(user_id: str = Depends(require_auth)):
    """
    Reset any stuck scanner runs in 'running' status.

    This is a debug endpoint to fix scans that got stuck when the app crashed.
    Sets all 'running' scans to 'failed' status so new scans can proceed.
    """
    uid = int(user_id)

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Find stuck scans
            cursor.execute("""
                SELECT id, source, started_at
                FROM scanner_runs
                WHERE user_id = %s AND status = 'running'
            """, (uid,))
            stuck_scans = cursor.fetchall()

            if not stuck_scans:
                return {"message": "No stuck scans found", "reset_count": 0}

            # Mark them as failed
            now_iso = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                UPDATE scanner_runs
                SET status = 'failed',
                    error_message = 'Reset by user - scan was stuck in running status',
                    completed_at = %s
                WHERE user_id = %s AND status = 'running'
            """, (now_iso, uid,))
            conn.commit()

            reset_sources = [dict(row) for row in stuck_scans]
            logger.info(f"Reset {len(stuck_scans)} stuck scans for user {uid}: {reset_sources}")

            return {
                "message": f"Reset {len(stuck_scans)} stuck scan(s)",
                "reset_count": len(stuck_scans),
                "sources": reset_sources
            }
    except Exception as e:
        logger.error(f"Error resetting stuck scans: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset stuck scans: {str(e)}"
        )


@router.get("/cache-stats")
async def get_cache_stats(user_id: str = Depends(require_auth)):
    """
    Get response cache statistics for debugging/observability.

    Returns hit/miss counts, cached keys, and TTL info.
    """
    return response_cache.stats()
