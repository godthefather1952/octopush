# Docker verification

Status: **PENDING EXTERNAL VERIFICATION.**

The image could not be built in the environment where Phase 0 remediation was
carried out. A Docker daemon was available and running, but the sandbox's
egress proxy denies the Docker Hub blob CDN, so no base image layer can be
pulled:

```
$ curl -sS -o /dev/null -w '%{http_code}\n' https://auth.docker.io/token
200
$ curl -sS -o /dev/null -w '%{http_code}\n' https://registry-1.docker.io/v2/
401                                    # expected: unauthenticated probe
$ curl -sS https://production.cloudfront.docker.com/
curl: (56) CONNECT tunnel failed, response 403
```

The registry API is reachable and the CDN that serves the layers is not, so
`FROM python:3.12-slim` fails at metadata resolution. This is an environment
policy limit, not a defect in the Dockerfile, and it is not something to work
around from inside the sandbox.

What *was* verified here, without a daemon:

- the packaging contract suite (`tests/contract/test_packaging.py`) — the
  image installs from `pyproject.toml` rather than a hand-copied list, both
  reachable extras are installed, the console scripts are declared, the
  container runs as non-root, and every `TF_` variable `docker-compose.yml`
  sets is one the settings loader actually reads;
- the checked-in migration applies to a real PostgreSQL 16 server and
  produces the schema the store expects (2 tables, `ts_ms`/`seq` BIGINT,
  `payload` JSONB);
- the full event-store contract suite passes against a database created by
  that migration rather than by the store's own DDL, so the two schema
  sources agree in practice and not just on inspection.

## What remains to be run where the registry is reachable

```bash
# 1. The image builds at all.
docker build -t trading-floor:phase0 .

# 2. The dependency set is complete — this is the check that would have
#    caught the missing `anthropic`, which fails lazily at first use rather
#    than at startup.
docker run --rm trading-floor:phase0 python -c "
import anthropic, asyncpg, fastapi, pydantic, redis, uvicorn, websockets
print('imports ok')"

# 3. The project is installed, not merely copied: the console scripts
#    declared in pyproject.toml must exist on PATH.
docker run --rm trading-floor:phase0 trading-floor --help
docker run --rm trading-floor:phase0 trading-floor-replay --help

# 4. Paper mode is enforced inside the image too.
docker run --rm -e TF_MODE=live trading-floor:phase0 \
    python -c "from core.config import load_settings; load_settings()"
#    Expected: non-zero exit, stderr naming TF_MODE and 'not supported'.

# 5. It runs as a non-root user.
docker run --rm trading-floor:phase0 id -u          # expect 10001

# 6. The default configuration starts and serves health.
docker run --rm -d --name tf-probe -p 8080:8080 trading-floor:phase0
sleep 25
curl -fsS http://127.0.0.1:8080/health
docker inspect --format '{{.State.Health.Status}}' tf-probe   # expect healthy
docker rm -f tf-probe

# 7. The full stack: Redis bus plus PostgreSQL event store, which is the
#    configuration docker-compose.yml selects and the one least covered by
#    the in-process test suite.
docker compose up -d --build
docker compose ps                    # redis, postgres, trading-floor healthy
sleep 30
curl -fsS http://127.0.0.1:8080/health
docker compose exec -T postgres psql -U trading -d trading_floor \
    -c "select count(*) from events;"      # expect a growing count
docker compose logs trading-floor | tail -50
docker compose down -v

# 8. The migration seeds a fresh container identically to the store's own
#    DDL. compose mounts storage/migrations into docker-entrypoint-initdb.d,
#    so this exercises the path a new deployment takes.
docker compose up -d postgres
sleep 10
docker compose exec -T postgres psql -U trading -d trading_floor -c "\dt"
#    Expected: exactly `events` and `sessions`. Any other table means the
#    migration and postgres_store.MIGRATION_SQL have diverged again.
docker compose down -v
```

Each step is a pass/fail with a stated expectation, so the result can be
recorded without judgement calls. Until they have been run on a host with
registry access, the Docker packaging should be treated as unverified.
