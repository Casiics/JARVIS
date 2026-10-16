# audit-keeper: signs the audit chain's head and exports WORM bundles (docs/runbooks/audit-hardening.md).
# Build context is the repository root (see docker/docker-compose.yml).
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PIP_NO_CACHE_DIR=1

COPY docker/audit-keeper/requirements.txt /tmp/requirements.txt
RUN pip install --no-deps --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY core/jarvis_core /app/jarvis_core
COPY docker/audit-keeper/keeper.sh /usr/local/bin/keeper
RUN chmod 0755 /usr/local/bin/keeper && install -d -o 10002 -g 10002 -m 0700 /worm

USER 10002:10002
ENTRYPOINT ["/usr/local/bin/keeper"]
