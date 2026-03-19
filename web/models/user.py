"""
User model for Seny web application.

Pydantic model for user data validation (not SQLAlchemy - we use raw SQLite).
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


class User(BaseModel):
    """
    User model for authentication and data association.

    This is a Pydantic model for validation, not a database model.
    Database operations are handled directly with sqlite3.
    """
    id: Optional[int] = None
    email: EmailStr
    hashed_password: str
    created_at: Optional[datetime] = None

    class Config:
        """Pydantic configuration."""
        # Allow datetime objects
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
