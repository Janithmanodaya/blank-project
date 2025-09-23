# Northflank/Container deployment for FastAPI app
# Uses Python 3.11 slim base and installs ffmpeg for media processing.

FROM python:3.11-slim

# Ensure Python output is unbuffered and UTF-8
ENV PYTHONUNBUFFERED=1

# System dependencies
# - ffmpeg: required for audio/video conversion used by yt-dlp and app flows
# - build-essential: sometimes needed for building wheels
# - curl/ca-certificates: for reliable HTTPS requests in some environments
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends ffmpeg build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency list and install first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application source
COPY . /app

# Create storage directory (can be mapped to a Northflank volume)
RUN mkdir -p /app/storage

# Expose the service port (Northflank will map this)
EXPOSE 8080

# Default environment (can be overridden by Northflank Secrets)
ENV HOST=0.0.0.0 \
    PORT=8080

# Start the FastAPI app using uvicorn (matches Render Procfile command)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]