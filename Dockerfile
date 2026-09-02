FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY . .

# Installed from pyproject.toml rather than a hand-copied list, which had
# already drifted: `anthropic` was missing, so a container started with
# TF_INTELLIGENCE_PROVIDER=claude — which docker-compose.yml exposes — came
# up healthy and then failed on LUMEN's first poll, because the SDK is
# imported lazily. Both extras are installed because both are reachable from
# configuration this image ships with.
#
# Installing the project itself (rather than relying on WORKDIR being on
# sys.path) is also what puts the `trading-floor` and `trading-floor-replay`
# console scripts declared in pyproject.toml on PATH.
RUN pip install --upgrade pip && \
    pip install ".[postgres,intelligence]"

RUN mkdir -p /app/data

# Non-root: nothing here needs privileges, and the process holds no secrets.
RUN useradd --create-home --uid 10001 trading && chown -R trading:trading /app
USER trading

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "apps.orchestrator"]
