"""
Rate limiting configuration for Seny API.

Provides a shared Limiter instance used by route modules and main.py.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
