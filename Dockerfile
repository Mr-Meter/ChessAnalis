FROM python:3.12-slim

# Stockfish from the Debian repository — cross-platform, no .exe needed.
# Installed to /usr/games/stockfish.
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (the DB and .env are provided via volume/environment, not copied into the image).
COPY *.py index.html openings.json ./
COPY img ./img

# Single worker — the engine pool already provides parallelism (see README).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
