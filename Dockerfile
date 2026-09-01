FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install "pydantic>=2.6" "fastapi>=0.110" "uvicorn[standard]>=0.27" \
                "websockets>=12.0" "httpx>=0.26" "redis>=5.0" "asyncpg>=0.29"

COPY . .

RUN mkdir -p /app/data

# Non-root: nothing here needs privileges, and the process holds no secrets.
RUN useradd --create-home --uid 10001 trading && chown -R trading:trading /app
USER trading

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "apps.orchestrator"]
