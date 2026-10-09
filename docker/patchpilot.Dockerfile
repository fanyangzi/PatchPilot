FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/src

# The verifier needs git to materialize pinned repository snapshots.  Install
# the project from the same source tree used by the clean-room command so a
# freshly built image exposes the API, CLI, adapters and test client rather
# than only a standalone pytest binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/patchpilot
COPY pyproject.toml uv.lock HANDOFF.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[api,dev]'

WORKDIR /workspace
ENTRYPOINT []
