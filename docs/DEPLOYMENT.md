# Deployment, operations and troubleshooting

## Production deployment (single VPS)

1. Ubuntu 22.04/24.04 server, 2 vCPU / 4 GB RAM / 40 GB disk. Install Docker Engine + Compose plugin.
2. `git clone` (or copy) the project, then:
   ```bash
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as ADMIN_TOKEN
   # set POSTGRES_PASSWORD, USER_AGENT (with your real contact), optional API keys
   docker compose up -d --build
   docker compose logs -f worker      # watch the first collection pass
   ```
3. Put a reverse proxy with HTTPS in front of port 8000 (Caddy is simplest):
   ```
   betwatch.example.com {
       reverse_proxy 127.0.0.1:8000 {
           flush_interval -1        # keep Server-Sent Events streaming
       }
   }
   ```
   Then bind the api to localhost only: change `"8000:8000"` to `"127.0.0.1:8000:8000"` in docker-compose.yml.
4. The `/admin` page and admin API are protected by `ADMIN_TOKEN`. Optionally also restrict `/admin` and
   `/api/admin` by IP or basic auth at the proxy.

## Upgrades

```bash
git pull && docker compose up -d --build   # the api container runs `alembic upgrade head` on start
```

After changing `app/models.py`, create a migration: `cd backend && alembic revision --autogenerate -m "describe change"`.

## Backups and recovery

```bash
# daily dump (cron)
docker compose exec -T db pg_dump -U betwatch -Fc betwatch > backups/betwatch-$(date +%F).dump
# restore into a fresh database
docker compose exec -T db pg_restore -U betwatch -d betwatch --clean < backups/betwatch-2026-10-25.dump
```

Keep at least 7 daily and 4 weekly dumps off the server (e.g. object storage). The database holds everything; the
containers are stateless.

## Monitoring

- `GET /api/health` — liveness (used by the Docker healthcheck).
- `GET /api/sources/health` — per-source last run, last success, consecutive failures, last error, latency, median
  collection delay. Alert if `sources_failing` > 0 for more than an hour.
- Admin → Recent collection runs — fetched / new / duplicate counts, errors, latency per run.
- Worker logs are JSON lines: `docker compose logs worker`.
- Failing sources back off automatically (interval × 2^failures, capped at ×32) and recover on the next success.

## Tuning

| Setting | Effect |
|---|---|
| Source `interval_minutes` (Admin) | How often each source is polled. Publisher RSS 10–15 min is polite; search sources 30–60 min |
| `SCHEDULER_TICK_SECONDS` | How often the worker checks for due sources |
| `ENRICH_MAX_PER_RUN` | Bylines resolved per tick (~2 s each) |
| `CLUSTER_THRESHOLD` (default 62) | Higher = stricter story grouping |
| `CLUSTER_WINDOW_HOURS` (default 96) | How far apart posts can be and still form one story |
| `RELEVANCE_FILTER` | `false` keeps every item from general feeds |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard says "Could not load data" | API not running or DB unreachable — `docker compose ps`, `docker compose logs api` |
| A source shows `HTTP 403` | The publisher blocks bots. Disable it; its stories usually still arrive via search sources |
| GDELT `429 rate limited` | Too many requests from your IP. Keep its interval ≥ 60 min; it paces 6 s between subjects |
| No journalists for a subject | Google results carry no byline; enrichment fills them over time (30/min). Check Admin runs for errors |
| Admin page says "Set ADMIN_TOKEN" | `ADMIN_TOKEN` is unset or still `change-me` |
| "N new posts" bar never appears | Proxy is buffering SSE — set `flush_interval -1` (Caddy) or `proxy_buffering off` (nginx) |
| `alembic upgrade` fails with "table already exists" | The DB was created by `create_all` (SQLite dev path). For PostgreSQL always start from an empty DB with Alembic |
| Duplicate-looking stories | Same event phrased very differently. Lower `CLUSTER_THRESHOLD` slightly (e.g. 58) |
