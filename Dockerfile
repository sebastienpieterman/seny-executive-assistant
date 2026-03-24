# Production Dockerfile for Seny web application
# Uses Python 3.11 slim image for smaller size and faster builds
# Cache bust: 2026-01-28 - Added Node.js for React frontend build

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (gcc for Python packages, curl for Node.js install)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 LTS for frontend build
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy frontend package files first for better layer caching
COPY web/frontend/package.json web/frontend/package-lock.json ./web/frontend/

# Install frontend dependencies
RUN cd web/frontend && npm ci

# Copy all application code
COPY web/ ./web/
COPY src/ ./src/
COPY start.sh ./

# Build React frontend
RUN cd web/frontend && npm run build

# Make start script executable
RUN chmod +x start.sh

# Expose port (Railway sets PORT env var dynamically)
EXPOSE 8000

# Health check - use PORT env var
# start-period gives the app time to initialize before health checks begin
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health').read()"

# Run the application using start script that reads PORT env var
CMD ["./start.sh"]
