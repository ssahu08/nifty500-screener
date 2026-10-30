# Alternative to render.yaml's native Python runtime (Render can deploy this
# Dockerfile instead; pick one approach when creating the service).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only - .env, data/, logs and tests stay out of the image.
COPY app ./app
RUN mkdir -p data

EXPOSE 8000

# $PORT is provided by Render (and most PaaS platforms).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
