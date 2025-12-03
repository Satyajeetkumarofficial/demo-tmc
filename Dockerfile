# Use official Python slim as base
FROM python:3.11-slim

# Install system deps: ffmpeg + optional image codecs for HEIC/WEBP
# Note: availability of libde265/libheif may depend on Debian version; adjust if apt fails.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    wget \
    ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# (Optional) Try to install HEIF/HEIC support packages if available
# If these packages are not available on your base image, the install will fail.
# Uncomment the following RUN line only if you tested it on your distro or need HEIC support.
# RUN apt-get update && apt-get install -y --no-install-recommends libheif1 libde265-0 && rm -rf /var/lib/apt/lists/*

# Create app dir
WORKDIR /app

# Copy requirements first (leverages Docker cache)
COPY requirements.txt /app/requirements.txt

# Install Python deps
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of the source
COPY . /app

# Create non-root user and ensure /app is writable
RUN useradd -ms /bin/bash botuser \
    && chown -R botuser:botuser /app

USER botuser
ENV HOME=/home/botuser
ENV PYTHONUNBUFFERED=1

# If you have a health server (health_server.py) and want it available on $PORT,
# set PORT env in Koyeb / platform. The bot script itself uses pyrogram and will run.
# Expose a port (optional)
EXPOSE 8080

# Start the bot (the script includes pyrogram idle() so it will keep running)
CMD ["python", "blaze_thumb_bot.py"]
