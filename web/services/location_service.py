"""
Location Service for Seny - Phase 7 (07-05)

Provides Google Location History management:
- Import from Google Takeout (ZIP or JSON)
- Search by place name or address
- Timeline views by date
- Frequent places analysis

Usage:
    location = LocationService(user_id)
    result = await location.import_takeout(file_content, "Takeout.zip")
    places = await location.search_locations("coffee")
    timeline = await location.get_timeline(date(2026, 1, 15))
"""

import io
import json
import logging
import uuid
import zipfile
from datetime import datetime, date, timedelta
from typing import Optional

from web.core.database import get_db

logger = logging.getLogger(__name__)


class LocationService:
    """
    Location history management service.

    Imports Google Takeout location data and provides query capabilities.
    One instance per user - do not share across users.

    Attributes:
        user_id: The user's database ID
    """

    def __init__(self, user_id: int):
        """
        Initialize Location service for a specific user.

        Args:
            user_id: User's database ID
        """
        self.user_id = user_id

    # =========================================================================
    # Import Operations
    # =========================================================================

    async def import_takeout(
        self,
        file_content: bytes,
        file_name: str
    ) -> dict:
        """
        Import location history from Google Takeout ZIP or JSON file.

        Handles both old format (Location History.json) and new format (Records.json).
        Deduplicates records based on timestamp + coordinates.

        Args:
            file_content: Raw file bytes
            file_name: Original filename (used to detect format)

        Returns:
            Dict with import_batch, records_imported, date_range_start, date_range_end
        """
        import_batch = str(uuid.uuid4())
        logger.info(f"Starting import batch {import_batch} from {file_name}")

        # Parse the file
        if file_name.lower().endswith('.zip'):
            records = await self._parse_takeout_zip(file_content)
        elif file_name.lower().endswith('.json'):
            data = json.loads(file_content.decode('utf-8'))
            records = await self._parse_location_data(data)
        else:
            raise ValueError(f"Unsupported file format: {file_name}")

        if not records:
            return {
                "import_batch": import_batch,
                "records_imported": 0,
                "date_range_start": None,
                "date_range_end": None
            }

        # Insert records in batches
        inserted_count = await self._insert_records(records, import_batch)

        # Calculate date range
        timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
        date_range_start = min(timestamps) if timestamps else None
        date_range_end = max(timestamps) if timestamps else None

        # Log the import
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO location_import_log
                (user_id, import_batch, file_name, records_imported, date_range_start, date_range_end)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                self.user_id,
                import_batch,
                file_name,
                inserted_count,
                date_range_start,
                date_range_end
            ))

        logger.info(f"Import complete: {inserted_count} records from {file_name}")

        return {
            "import_batch": import_batch,
            "records_imported": inserted_count,
            "date_range_start": date_range_start,
            "date_range_end": date_range_end
        }

    async def _parse_takeout_zip(self, file_content: bytes) -> list[dict]:
        """
        Extract and parse location data from Takeout ZIP.

        Looks for:
        - Takeout/Location History/Records.json (new format)
        - Takeout/Location History/Location History.json (old format)

        Args:
            file_content: ZIP file bytes

        Returns:
            List of parsed location records
        """
        with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
            # Try new format first
            for name in zf.namelist():
                if 'Records.json' in name:
                    with zf.open(name) as f:
                        data = json.load(f)
                        return await self._parse_location_data(data)

            # Try old format
            for name in zf.namelist():
                if 'Location History.json' in name:
                    with zf.open(name) as f:
                        data = json.load(f)
                        return await self._parse_location_data(data)

        logger.warning("No location history file found in ZIP")
        return []

    async def _parse_location_data(self, data: dict) -> list[dict]:
        """
        Parse location data from either old or new Takeout format.

        Args:
            data: Parsed JSON data

        Returns:
            List of normalized location records
        """
        records = []

        # Handle new format (has "locations" array)
        if "locations" in data:
            for loc in data["locations"]:
                record = self._parse_new_format_record(loc)
                if record:
                    records.append(record)

        # Handle old format (has "locationHistory" or is a list)
        elif "locationHistory" in data:
            for loc in data["locationHistory"]:
                record = self._parse_old_format_record(loc)
                if record:
                    records.append(record)

        # Sometimes old format is just a list of locations
        elif isinstance(data, list):
            for loc in data:
                record = self._parse_old_format_record(loc)
                if record:
                    records.append(record)

        logger.info(f"Parsed {len(records)} location records")
        return records

    def _parse_new_format_record(self, loc: dict) -> Optional[dict]:
        """
        Parse a record from new Takeout format (Records.json).

        Args:
            loc: Single location object from Takeout

        Returns:
            Normalized record dict or None if invalid
        """
        try:
            # Convert E7 coordinates to decimal
            lat = loc.get("latitudeE7")
            lng = loc.get("longitudeE7")

            if lat is None or lng is None:
                return None

            latitude = lat / 10_000_000
            longitude = lng / 10_000_000

            # Parse timestamp
            timestamp_str = loc.get("timestamp")
            if not timestamp_str:
                return None

            # Handle various timestamp formats
            timestamp = self._parse_timestamp(timestamp_str)
            if not timestamp:
                return None

            record = {
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": loc.get("accuracy"),
                "timestamp": timestamp,
                "source": loc.get("source")
            }

            # Extract place visit info if available
            place_visit = loc.get("placeVisit", {})
            if place_visit:
                place_loc = place_visit.get("location", {})
                record["place_id"] = place_loc.get("placeId")
                record["place_name"] = place_loc.get("name")
                record["address"] = place_loc.get("address")

                # Calculate duration
                duration = place_visit.get("duration", {})
                start = duration.get("startTimestamp")
                end = duration.get("endTimestamp")
                if start and end:
                    start_dt = self._parse_timestamp(start)
                    end_dt = self._parse_timestamp(end)
                    if start_dt and end_dt:
                        minutes = int((end_dt - start_dt).total_seconds() / 60)
                        record["duration_minutes"] = minutes

            return record

        except Exception as e:
            logger.debug(f"Failed to parse record: {e}")
            return None

    def _parse_old_format_record(self, loc: dict) -> Optional[dict]:
        """
        Parse a record from old Takeout format (Location History.json).

        Args:
            loc: Single location object from Takeout

        Returns:
            Normalized record dict or None if invalid
        """
        try:
            # Old format also uses E7 coordinates
            lat = loc.get("latitudeE7")
            lng = loc.get("longitudeE7")

            if lat is None or lng is None:
                return None

            latitude = lat / 10_000_000
            longitude = lng / 10_000_000

            # Old format has timestampMs (milliseconds since epoch)
            timestamp_ms = loc.get("timestampMs")
            if timestamp_ms:
                timestamp = datetime.fromtimestamp(int(timestamp_ms) / 1000)
            else:
                # Try other timestamp formats
                timestamp_str = loc.get("timestamp")
                if timestamp_str:
                    timestamp = self._parse_timestamp(timestamp_str)
                else:
                    return None

            if not timestamp:
                return None

            return {
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": loc.get("accuracy"),
                "timestamp": timestamp,
                "source": loc.get("source"),
                "place_id": None,
                "place_name": None,
                "address": None,
                "duration_minutes": None
            }

        except Exception as e:
            logger.debug(f"Failed to parse old format record: {e}")
            return None

    def _parse_timestamp(self, ts: str) -> Optional[datetime]:
        """
        Parse various timestamp formats from Takeout.

        Args:
            ts: Timestamp string

        Returns:
            datetime object or None if parsing fails
        """
        formats = [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue

        return None

    async def _insert_records(self, records: list[dict], import_batch: str) -> int:
        """
        Insert location records in batches.

        Uses INSERT OR IGNORE to handle duplicates based on
        (user_id, timestamp, latitude, longitude) unique constraint.

        Args:
            records: List of location records to insert
            import_batch: UUID for this import

        Returns:
            Number of records actually inserted
        """
        BATCH_SIZE = 1000
        total_inserted = 0

        with get_db() as conn:
            cursor = conn.cursor()

            for i in range(0, len(records), BATCH_SIZE):
                batch = records[i:i + BATCH_SIZE]

                for record in batch:
                    try:
                        cursor.execute("""
                            INSERT OR IGNORE INTO location_history
                            (user_id, latitude, longitude, accuracy, timestamp,
                             place_id, place_name, address, duration_minutes, source, import_batch)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            self.user_id,
                            record["latitude"],
                            record["longitude"],
                            record.get("accuracy"),
                            record["timestamp"],
                            record.get("place_id"),
                            record.get("place_name"),
                            record.get("address"),
                            record.get("duration_minutes"),
                            record.get("source"),
                            import_batch
                        ))

                        if cursor.rowcount > 0:
                            total_inserted += 1

                    except Exception as e:
                        logger.debug(f"Failed to insert record: {e}")

                logger.debug(f"Processed batch {i // BATCH_SIZE + 1}")

        return total_inserted

    async def get_import_history(self) -> list[dict]:
        """
        Get list of past location imports.

        Returns:
            List of import log entries
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, import_batch, file_name, records_imported,
                       date_range_start, date_range_end, created_at
                FROM location_import_log
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (self.user_id,))

            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Query Operations
    # =========================================================================

    async def search_locations(
        self,
        query: str,
        limit: int = 20
    ) -> list[dict]:
        """
        Search locations by place name or address.

        Args:
            query: Search term
            limit: Maximum results

        Returns:
            List of matching location records
        """
        with get_db() as conn:
            cursor = conn.cursor()
            search_term = f"%{query}%"

            cursor.execute("""
                SELECT id, latitude, longitude, accuracy, timestamp,
                       place_id, place_name, address, duration_minutes, source
                FROM location_history
                WHERE user_id = %s
                  AND (place_name LIKE %s OR address LIKE %s)
                ORDER BY timestamp DESC
                LIMIT %s
            """, (self.user_id, search_term, search_term, limit))

            return [dict(row) for row in cursor.fetchall()]

    async def get_locations_by_date(
        self,
        target_date: date
    ) -> list[dict]:
        """
        Get all locations for a specific date.

        Args:
            target_date: Date to query

        Returns:
            List of locations, ordered chronologically
        """
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, latitude, longitude, accuracy, timestamp,
                       place_id, place_name, address, duration_minutes, source
                FROM location_history
                WHERE user_id = %s
                  AND timestamp >= %s AND timestamp <= %s
                ORDER BY timestamp ASC
            """, (self.user_id, start, end))

            return [dict(row) for row in cursor.fetchall()]

    async def get_locations_by_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 100
    ) -> list[dict]:
        """
        Get locations within a date range.

        Args:
            start: Start datetime
            end: End datetime
            limit: Maximum results

        Returns:
            List of locations, ordered chronologically
        """
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, latitude, longitude, accuracy, timestamp,
                       place_id, place_name, address, duration_minutes, source
                FROM location_history
                WHERE user_id = %s
                  AND timestamp >= %s AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT %s
            """, (self.user_id, start, end, limit))

            return [dict(row) for row in cursor.fetchall()]

    async def get_timeline(
        self,
        target_date: date
    ) -> list[dict]:
        """
        Get timeline of place visits for a date.

        Returns only entries with place names, grouped by visits.

        Args:
            target_date: Date to query

        Returns:
            List of place visits with time and duration
        """
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, latitude, longitude, timestamp,
                       place_id, place_name, address, duration_minutes
                FROM location_history
                WHERE user_id = %s
                  AND timestamp >= %s AND timestamp <= %s
                  AND place_name IS NOT NULL
                ORDER BY timestamp ASC
            """, (self.user_id, start, end))

            return [dict(row) for row in cursor.fetchall()]

    async def get_place_visits(
        self,
        place_name: str = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Get visits to a specific place, or most visited places.

        Args:
            place_name: Optional place to filter by
            limit: Maximum results

        Returns:
            If place_name given: List of visits to that place
            If not: List of most frequently visited places
        """
        with get_db() as conn:
            cursor = conn.cursor()

            if place_name:
                # Get visits to specific place
                search_term = f"%{place_name}%"
                cursor.execute("""
                    SELECT id, latitude, longitude, timestamp,
                           place_id, place_name, address, duration_minutes
                    FROM location_history
                    WHERE user_id = %s
                      AND place_name LIKE %s
                    ORDER BY timestamp DESC
                    LIMIT %s
                """, (self.user_id, search_term, limit))

                return [dict(row) for row in cursor.fetchall()]
            else:
                # Get most visited places
                cursor.execute("""
                    SELECT place_name, COUNT(*) as visit_count,
                           MAX(timestamp) as last_visit,
                           address
                    FROM location_history
                    WHERE user_id = %s
                      AND place_name IS NOT NULL
                    GROUP BY place_name
                    ORDER BY visit_count DESC
                    LIMIT %s
                """, (self.user_id, limit))

                return [dict(row) for row in cursor.fetchall()]

    async def get_location_stats(
        self,
        days: int = 30
    ) -> dict:
        """
        Get statistics about location history.

        Args:
            days: Number of days to analyze

        Returns:
            Dict with total_records, total_places, date_range, etc.
        """
        cutoff = datetime.now() - timedelta(days=days)

        with get_db() as conn:
            cursor = conn.cursor()

            # Total records
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM location_history
                WHERE user_id = %s
            """, (self.user_id,))
            total_records = cursor.fetchone()["total"]

            # Records in date range
            cursor.execute("""
                SELECT COUNT(*) as recent
                FROM location_history
                WHERE user_id = %s AND timestamp >= %s
            """, (self.user_id, cutoff))
            recent_records = cursor.fetchone()["recent"]

            # Unique places
            cursor.execute("""
                SELECT COUNT(DISTINCT place_name) as places
                FROM location_history
                WHERE user_id = %s AND place_name IS NOT NULL
            """, (self.user_id,))
            unique_places = cursor.fetchone()["places"]

            # Date range
            cursor.execute("""
                SELECT MIN(timestamp) as earliest, MAX(timestamp) as latest
                FROM location_history
                WHERE user_id = %s
            """, (self.user_id,))
            date_range = cursor.fetchone()

            # Top places (last N days)
            cursor.execute("""
                SELECT place_name, COUNT(*) as visits
                FROM location_history
                WHERE user_id = %s
                  AND timestamp >= %s
                  AND place_name IS NOT NULL
                GROUP BY place_name
                ORDER BY visits DESC
                LIMIT 5
            """, (self.user_id, cutoff))
            top_places = [dict(row) for row in cursor.fetchall()]

            return {
                "total_records": total_records,
                "recent_records": recent_records,
                "unique_places": unique_places,
                "earliest_date": date_range["earliest"],
                "latest_date": date_range["latest"],
                "top_places": top_places,
                "analysis_days": days
            }

    # =========================================================================
    # Delete Operations
    # =========================================================================

    async def delete_all_locations(self) -> int:
        """
        Delete all location history for this user.

        Returns:
            Number of records deleted
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM location_history
                WHERE user_id = %s
            """, (self.user_id,))
            deleted = cursor.rowcount

            # Also clear import log
            cursor.execute("""
                DELETE FROM location_import_log
                WHERE user_id = %s
            """, (self.user_id,))

            logger.info(f"Deleted {deleted} location records for user {self.user_id}")
            return deleted

    async def delete_import_batch(self, import_batch: str) -> int:
        """
        Delete a specific import batch.

        Args:
            import_batch: UUID of the import to delete

        Returns:
            Number of records deleted
        """
        with get_db() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM location_history
                WHERE user_id = %s AND import_batch = %s
            """, (self.user_id, import_batch))
            deleted = cursor.rowcount

            cursor.execute("""
                DELETE FROM location_import_log
                WHERE user_id = %s AND import_batch = %s
            """, (self.user_id, import_batch))

            logger.info(f"Deleted import batch {import_batch}: {deleted} records")
            return deleted

    # =========================================================================
    # Formatting Helpers
    # =========================================================================

    def format_location(self, loc: dict) -> str:
        """
        Format a location record for display.

        Args:
            loc: Location dict from query

        Returns:
            Human-readable location string
        """
        parts = []

        if loc.get("place_name"):
            parts.append(f"📍 {loc['place_name']}")
        else:
            parts.append(f"📍 {loc['latitude']:.6f}, {loc['longitude']:.6f}")

        if loc.get("address"):
            parts.append(f"   {loc['address']}")

        if loc.get("timestamp"):
            ts = loc["timestamp"]
            if isinstance(ts, str):
                parts.append(f"   🕐 {ts}")
            else:
                parts.append(f"   🕐 {ts.strftime('%Y-%m-%d %H:%M')}")

        if loc.get("duration_minutes"):
            parts.append(f"   ⏱️ {loc['duration_minutes']} minutes")

        return "\n".join(parts)

    def format_timeline(self, timeline: list[dict]) -> str:
        """
        Format a day's timeline for display.

        Args:
            timeline: List of place visits

        Returns:
            Formatted timeline string
        """
        if not timeline:
            return "No location data for this date."

        lines = []
        for loc in timeline:
            ts = loc.get("timestamp")
            if isinstance(ts, str):
                time_str = ts.split("T")[1][:5] if "T" in ts else ts
            else:
                time_str = ts.strftime("%H:%M")

            place = loc.get("place_name", "Unknown location")
            duration = loc.get("duration_minutes")

            if duration:
                lines.append(f"{time_str} - {place} ({duration} min)")
            else:
                lines.append(f"{time_str} - {place}")

        return "\n".join(lines)
