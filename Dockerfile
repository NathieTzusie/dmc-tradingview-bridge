# ─────────────────────────────────────────────────────────────
# DMC TradingView Bridge — Multi-user Docker Image
# MIT License
# ─────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# Install system deps (ccxt needs libffi, sqlite)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application
COPY sisie_bridge/ sisie_bridge/

# Default config
COPY configs/bridge.yaml configs/bridge.yaml

# Webhook port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Entry point
ENTRYPOINT ["python", "-m", "sisie_bridge.main", "--config", "configs/bridge.yaml"]
