"""
Location History endpoints for Seny - Phase 7 (07-05).

Google Takeout location history import and query API:
- POST /api/location/import - Upload Takeout ZIP/JSON
- GET /api/location/imports - Get import history
- GET /api/location/search - Search locations by place name
- GET /api/location/date/{date} - Get locations for a date
- GET /api/location/timeline/{date} - Get timeline for a date
- GET /api/location/places - Get most visited places
- GET /api/location/stats - Get location statistics
- DELETE /api/location - Delete all location data
- DELETE /api/location/import/{batch_id} - Delete specific import
"""

import logging
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends, Query, UploadFile, File
from pydantic import BaseModel

from web.auth.jwt_utils import require_auth
from web.services.location_service import LocationService

logger = logging.getLogger(__name__)


# Create location router
router = APIRouter()


# ============================================================================
# Response Models
# ============================================================================

class LocationRecord(BaseModel):
    """Single location record."""
    id: int
    latitude: float
    longitude: float
    accuracy: Optional[int]
    timestamp: str
    place_id: Optional[str]
    place_name: Optional[str]
    address: Optional[str]
    duration_minutes: Optional[int]
    source: Optional[str]


class LocationSearchResponse(BaseModel):
    """Response for location search."""
    locations: list[LocationRecord]
    total: int
    query: str


class LocationListResponse(BaseModel):
    """Response for location listing."""
    locations: list[LocationRecord]
    total: int
    date: Optional[str] = None


class PlaceVisit(BaseModel):
    """Place visit record."""
    place_name: str
    visit_count: int
    last_visit: str
    address: Optional[str]


class PlaceVisitsResponse(BaseModel):
    """Response for place visits."""
    places: list[PlaceVisit]
    total: int


class TopPlace(BaseModel):
    """Top place for stats."""
    place_name: str
    visits: int


class LocationStatsResponse(BaseModel):
    """Response for location statistics."""
    total_records: int
    recent_records: int
    unique_places: int
    earliest_date: Optional[str]
    latest_date: Optional[str]
    top_places: list[TopPlace]
    analysis_days: int


class ImportResult(BaseModel):
    """Result of a location import."""
    import_batch: str
    records_imported: int
    date_range_start: Optional[str]
    date_range_end: Optional[str]


class ImportLogEntry(BaseModel):
    """Import log entry."""
    id: int
    import_batch: str
    file_name: Optional[str]
    records_imported: int
    date_range_start: Optional[str]
    date_range_end: Optional[str]
    created_at: str


class ImportHistoryResponse(BaseModel):
    """Response for import history."""
    imports: list[ImportLogEntry]
    total: int


class DeleteResponse(BaseModel):
    """Response for delete operations."""
    deleted_count: int
    message: str


# ============================================================================
# Helper Functions
# ============================================================================

def format_timestamp(ts) -> str:
    """Format a timestamp to ISO string."""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def location_to_record(loc: dict) -> LocationRecord:
    """Convert a location dict to LocationRecord."""
    return LocationRecord(
        id=loc["id"],
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        accuracy=loc.get("accuracy"),
        timestamp=format_timestamp(loc.get("timestamp")),
        place_id=loc.get("place_id"),
        place_name=loc.get("place_name"),
        address=loc.get("address"),
        duration_minutes=loc.get("duration_minutes"),
        source=loc.get("source")
    )


# ============================================================================
# Import Endpoints
# ============================================================================

@router.post("/import", response_model=ImportResult)
async def import_location_history(
    file: UploadFile = File(..., description="Google Takeout ZIP or JSON file"),
    user_id: int = Depends(require_auth)
):
    """
    Import location history from Google Takeout.

    Accepts either:
    - A ZIP file from Google Takeout containing location history
    - A JSON file (Records.json or Location History.json)

    Duplicate records (same timestamp + coordinates) are automatically skipped.

    Args:
        file: Uploaded file
        user_id: Authenticated user ID

    Returns:
        ImportResult with import batch ID and count of imported records
    """
    try:
        # Validate file type
        filename = file.filename.lower()
        if not (filename.endswith('.zip') or filename.endswith('.json')):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File must be a .zip or .json file"
            )

        # Read file content
        content = await file.read()

        if len(content) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is empty"
            )

        # Import the data
        location_service = LocationService(user_id)
        result = await location_service.import_takeout(content, file.filename)

        return ImportResult(
            import_batch=result["import_batch"],
            records_imported=result["records_imported"],
            date_range_start=format_timestamp(result.get("date_range_start")),
            date_range_end=format_timestamp(result.get("date_range_end"))
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Location import failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Import failed: {str(e)}"
        )


@router.get("/imports", response_model=ImportHistoryResponse)
async def get_import_history(
    user_id: int = Depends(require_auth)
):
    """
    Get list of past location imports.

    Returns all import operations with their metadata.

    Args:
        user_id: Authenticated user ID

    Returns:
        ImportHistoryResponse with list of imports
    """
    try:
        location_service = LocationService(user_id)
        imports = await location_service.get_import_history()

        entries = [
            ImportLogEntry(
                id=imp["id"],
                import_batch=imp["import_batch"],
                file_name=imp.get("file_name"),
                records_imported=imp["records_imported"],
                date_range_start=format_timestamp(imp.get("date_range_start")),
                date_range_end=format_timestamp(imp.get("date_range_end")),
                created_at=format_timestamp(imp["created_at"])
            )
            for imp in imports
        ]

        return ImportHistoryResponse(
            imports=entries,
            total=len(entries)
        )

    except Exception as e:
        logger.error(f"Get import history failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get import history: {str(e)}"
        )


# ============================================================================
# Query Endpoints
# ============================================================================

@router.get("/search", response_model=LocationSearchResponse)
async def search_locations(
    q: str = Query(..., description="Search query (place name or address)"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    user_id: int = Depends(require_auth)
):
    """
    Search locations by place name or address.

    Args:
        q: Search query
        limit: Maximum results
        user_id: Authenticated user ID

    Returns:
        LocationSearchResponse with matching locations
    """
    try:
        location_service = LocationService(user_id)
        results = await location_service.search_locations(q, limit)

        locations = [location_to_record(loc) for loc in results]

        return LocationSearchResponse(
            locations=locations,
            total=len(locations),
            query=q
        )

    except Exception as e:
        logger.error(f"Location search failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )


@router.get("/date/{target_date}", response_model=LocationListResponse)
async def get_locations_by_date(
    target_date: date,
    user_id: int = Depends(require_auth)
):
    """
    Get all locations for a specific date.

    Args:
        target_date: Date to query (YYYY-MM-DD)
        user_id: Authenticated user ID

    Returns:
        LocationListResponse with all locations for that date
    """
    try:
        location_service = LocationService(user_id)
        results = await location_service.get_locations_by_date(target_date)

        locations = [location_to_record(loc) for loc in results]

        return LocationListResponse(
            locations=locations,
            total=len(locations),
            date=target_date.isoformat()
        )

    except Exception as e:
        logger.error(f"Get locations by date failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get locations: {str(e)}"
        )


@router.get("/timeline/{target_date}", response_model=LocationListResponse)
async def get_timeline(
    target_date: date,
    user_id: int = Depends(require_auth)
):
    """
    Get timeline of place visits for a date.

    Returns only entries with place names, useful for seeing
    "where did I go today" type queries.

    Args:
        target_date: Date to query (YYYY-MM-DD)
        user_id: Authenticated user ID

    Returns:
        LocationListResponse with place visits timeline
    """
    try:
        location_service = LocationService(user_id)
        results = await location_service.get_timeline(target_date)

        locations = [location_to_record(loc) for loc in results]

        return LocationListResponse(
            locations=locations,
            total=len(locations),
            date=target_date.isoformat()
        )

    except Exception as e:
        logger.error(f"Get timeline failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get timeline: {str(e)}"
        )


@router.get("/places", response_model=PlaceVisitsResponse)
async def get_place_visits(
    place_name: Optional[str] = Query(None, description="Filter by place name"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    user_id: int = Depends(require_auth)
):
    """
    Get most visited places, or visits to a specific place.

    If place_name is provided, returns visits to that place.
    Otherwise, returns the most frequently visited places.

    Args:
        place_name: Optional place name filter
        limit: Maximum results
        user_id: Authenticated user ID

    Returns:
        PlaceVisitsResponse with place visit data
    """
    try:
        location_service = LocationService(user_id)
        results = await location_service.get_place_visits(place_name, limit)

        if place_name:
            # Results are individual visits
            # Convert to aggregated format for consistency
            if results:
                places = [
                    PlaceVisit(
                        place_name=results[0].get("place_name", place_name),
                        visit_count=len(results),
                        last_visit=format_timestamp(results[0].get("timestamp")),
                        address=results[0].get("address")
                    )
                ]
            else:
                places = []
        else:
            # Results are already aggregated
            places = [
                PlaceVisit(
                    place_name=p["place_name"],
                    visit_count=p["visit_count"],
                    last_visit=format_timestamp(p.get("last_visit")),
                    address=p.get("address")
                )
                for p in results
            ]

        return PlaceVisitsResponse(
            places=places,
            total=len(places)
        )

    except Exception as e:
        logger.error(f"Get place visits failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get place visits: {str(e)}"
        )


@router.get("/stats", response_model=LocationStatsResponse)
async def get_location_stats(
    days: int = Query(30, ge=1, le=365, description="Days to analyze"),
    user_id: int = Depends(require_auth)
):
    """
    Get location statistics.

    Args:
        days: Number of days to analyze for "recent" stats
        user_id: Authenticated user ID

    Returns:
        LocationStatsResponse with statistics
    """
    try:
        location_service = LocationService(user_id)
        stats = await location_service.get_location_stats(days)

        return LocationStatsResponse(
            total_records=stats["total_records"],
            recent_records=stats["recent_records"],
            unique_places=stats["unique_places"],
            earliest_date=format_timestamp(stats.get("earliest_date")),
            latest_date=format_timestamp(stats.get("latest_date")),
            top_places=[
                TopPlace(place_name=p["place_name"], visits=p["visits"])
                for p in stats.get("top_places", [])
            ],
            analysis_days=stats["analysis_days"]
        )

    except Exception as e:
        logger.error(f"Get location stats failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get statistics: {str(e)}"
        )


# ============================================================================
# Delete Endpoints
# ============================================================================

@router.delete("", response_model=DeleteResponse)
async def delete_all_locations(
    user_id: int = Depends(require_auth)
):
    """
    Delete all location history for the user.

    This is a destructive operation and cannot be undone.

    Args:
        user_id: Authenticated user ID

    Returns:
        DeleteResponse with count of deleted records
    """
    try:
        location_service = LocationService(user_id)
        deleted_count = await location_service.delete_all_locations()

        return DeleteResponse(
            deleted_count=deleted_count,
            message=f"Deleted {deleted_count} location records"
        )

    except Exception as e:
        logger.error(f"Delete all locations failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete locations: {str(e)}"
        )


@router.delete("/import/{batch_id}", response_model=DeleteResponse)
async def delete_import_batch(
    batch_id: str,
    user_id: int = Depends(require_auth)
):
    """
    Delete a specific import batch.

    Args:
        batch_id: The import batch UUID to delete
        user_id: Authenticated user ID

    Returns:
        DeleteResponse with count of deleted records
    """
    try:
        location_service = LocationService(user_id)
        deleted_count = await location_service.delete_import_batch(batch_id)

        return DeleteResponse(
            deleted_count=deleted_count,
            message=f"Deleted {deleted_count} location records from batch {batch_id}"
        )

    except Exception as e:
        logger.error(f"Delete import batch failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete import batch: {str(e)}"
        )
