# private-assistant: the private domain's executor and local workers (docs/runbooks/private-domain.md).
# Build context is the repository root (see docker/docker-compose.yml).
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app PIP_NO_CACHE_DIR=1

COPY modules/mod-private/requirements.txt /tmp/requirements.txt
RUN pip install --no-deps --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY core/jarvis_core /app/jarvis_core
COPY modules/mod-private/mod_private /app/mod_private

# The data volume inherits this owner and mode on first use.
RUN install -d -o 10001 -g 10001 -m 0700 /var/lib/jarvis/private

USER 10001:10001
WORKDIR /var/lib/jarvis/private
ENTRYPOINT ["python", "-m", "mod_private"]
