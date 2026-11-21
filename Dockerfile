FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# System deps for bcrypt/cryptography builds (slim base)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential libffi-dev libssl-dev \
    && apt-get autoremove -y

# Platform app + core gateway
COPY platform_app/ ./platform_app/
COPY gemini_web2api/ ./gemini_web2api/
COPY run_platform.py ./

# Static assets served by the platform
COPY logo.png logo-mark.png logo-lockup.png gemini-connector.zip ./
COPY gemini-cookie-sync-extension/ ./gemini-cookie-sync-extension/

# Data dir is created at runtime; mount a volume to persist it.
RUN mkdir -p /app/data

EXPOSE 8081

CMD ["python", "run_platform.py", "--host", "0.0.0.0", "--port", "8081"]
