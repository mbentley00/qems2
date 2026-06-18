# QEMS2 production image — Django 6 / Python 3.14 under Gunicorn.
# Pinned to 3.14 because the app targets it and App Service's built-in
# runtimes may not offer it; a container makes the runtime explicit.
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# psycopg2 needs libpq at runtime; build-essential + libpq-dev to compile the
# wheel if a prebuilt one isn't available for 3.14. ffmpeg is needed by the
# packet-to-MP3 feature to stitch/encode audio segments.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Static files are baked into the image (WhiteNoise serves them). Uses a
# throwaway SECRET_KEY so collectstatic can run without real config.
RUN SECRET_KEY=build-time-only DJANGO_SETTINGS_MODULE=qems2.settings \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Normalize line endings (entrypoint.sh may be saved CRLF on Windows) and make
# it executable, then use it for startup: migrate -> bootstrap -> index -> serve.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
