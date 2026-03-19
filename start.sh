#!/bin/bash
# Startup script for Seny web application
# Handles Railway's dynamic PORT environment variable

PORT=${PORT:-8000}
exec uvicorn web.main:app --host 0.0.0.0 --port $PORT --log-level info
