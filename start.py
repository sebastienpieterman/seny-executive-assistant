#!/usr/bin/env python3
"""Startup script for Seny web application."""
import os
import sys

# Get port from environment variable, default to 8000
port = os.getenv("PORT", "8000")

print(f"Starting Seny on port {port}")
sys.stdout.flush()

# Start uvicorn
os.execvp("uvicorn", ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", port])
