"""
Services for Seny web application.
"""

from web.services.notes_service import NotesService
from web.services.history_service import HistoryService
from web.services.location_service import LocationService
from web.services.people_service import PeopleService
from web.services.projects_service import ProjectsService
from web.services.digest_service import DigestService

__all__ = [
    "NotesService",
    "HistoryService",
    "LocationService",
    "PeopleService",
    "ProjectsService",
    "DigestService",
]
