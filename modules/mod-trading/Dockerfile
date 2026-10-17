# trading-paper: paper trading on live bars (docs/runbooks/trading-paper.md). Paper only: the
# executor refuses any broker but the paper broker, and the image holds no broker key.
# Build context is the repository root (see docker/docker-compose.yml).
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PIP_NO_CACHE_DIR=1 \
    JARVIS_TRADING_DATA=/var/lib/jarvis/trading

COPY modules/mod-trading/requirements.txt /tmp/requirements.txt
RUN pip install --no-deps --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY core/jarvis_core /app/jarvis_core
COPY modules/mod-trading/risk/trading_risk /app/trading_risk
COPY modules/mod-trading/risk/limits.json /app/risk/limits.json
COPY modules/mod-trading/mod_trading /app/mod_trading

# The data volume inherits this owner and mode on first use.
RUN install -d -o 10003 -g 10003 -m 0700 /var/lib/jarvis/trading

USER 10003:10003
WORKDIR /var/lib/jarvis/trading
ENTRYPOINT ["python", "-m", "mod_trading"]
