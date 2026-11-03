# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for Fincas (Generic Python 3.12 + ASGI + WeasyPrint)

# -----------------------------------------------------------------------------
# Stage 1: Build virtualenv with C-extensions
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Build dependencies for C-extensions (psycopg2, cffi, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create isolated virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements/ /build/requirements/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /build/requirements/prod.txt

# -----------------------------------------------------------------------------
# Stage 2: Runtime Image (Minimal, Non-root, WeasyPrint-ready)
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Install runtime shared libraries:
# - libpq5: PostgreSQL runtime client
# - libcairo2, libpango*, libgdk-pixbuf*: WeasyPrint PDF generator
# - fonts-dejavu-core, fontconfig: PDF typography & font rendering
# - shared-mime-info: MIME detection
# - curl: container healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-dejavu-core \
    fontconfig \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root system user
RUN groupadd -g 10001 golden && \
    useradd -u 10001 -g golden -s /bin/bash -m golden

WORKDIR /app

# Copy python virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source code
COPY --chown=golden:golden . /app

# Create staticfiles and media directories with proper ownership
RUN mkdir -p /app/staticfiles /app/media && \
    chown -R golden:golden /app/staticfiles /app/media && \
    chmod +x /app/scripts/entrypoint.sh

# Collect static assets for production during image build
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_SECRET_KEY=build-static-placeholder \
    DATABASE_URL=sqlite:///tmp/build.db \
    python manage.py collectstatic --noinput --clear && \
    chown -R golden:golden /app/staticfiles

USER golden

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
