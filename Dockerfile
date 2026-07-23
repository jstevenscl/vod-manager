# ── Stage 1: Build React frontend ────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python backend ───────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /frontend/dist ./static

ARG GIT_SHA=dev
ARG GIT_REF=local
ENV GIT_SHA=$GIT_SHA
ENV GIT_REF=$GIT_REF

ENV PYTHONUNBUFFERED=1

EXPOSE 8282

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8282"]
