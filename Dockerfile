# Research Intelligence Dashboard — single-stage image.
# Entry point is `python run.py` (uvicorn on :8000 + an in-process APScheduler).
# Deps are small and pure-Python; one build stage is enough (no base/code split
# like LogiqGPT needs for sentence-transformers/chromadb).
FROM python:3.12-slim

# curl for the HEALTHCHECK; no other system libs are required by requirements.txt.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the layer caches when only source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite gotcha (same one LogiqGPT solved): the research.db lives on LOCAL disk,
# NOT on the Azure Files (SMB) mount — SMB breaks SQLite WAL (lock stalls/corruption).
# The deploy script sets DATABASE_URL=sqlite:////app/local/research.db and seeds
# /app/local from the Azure Files share at startup. /app/data is the persistent share.
RUN mkdir -p /app/local /app/data

EXPOSE 8000

# In-process APScheduler: the container MUST stay warm (min-replicas=1, no scale-to-zero)
# or the scheduler thread dies and ingestion stops. See azure-deploy-dashboard.sh.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail http://localhost:8000/api/config || exit 1

CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
