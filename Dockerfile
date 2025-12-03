FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Copy requirements and bot
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy project files
COPY . /app

# Ensure /app is owned by the non-root user that will run the bot
RUN useradd -ms /bin/bash botuser \
    && chown -R botuser:botuser /app

USER botuser
ENV HOME=/home/botuser

CMD ["python", "blaze_thumb_bot.py"]
