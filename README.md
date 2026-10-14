# Bike Auction Platform

Live bidding on used motorcycles. FastAPI + SQLAlchemy + WebSockets backend, React frontend served by the same app.

**Live URL:** `<add after deploy>`
**Demo logins:** admin `admin@vutto.test` / `Admin@12345` · buyer `buyer@vutto.test` / `Buyer@12345`

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # run tests
```
Defaults to SQLite. Set `DATABASE_URL` for Postgres. Docker: `docker build -t auction . && docker run -p 8000:8000 auction`.

| Env var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres URL (falls back to SQLite) |
| `JWT_SECRET` | **Set to a long random string in production** |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Seeded admin (first boot only) |

## Deploy (Render)
1. Push this folder to a GitHub repo.
2. Render → New → PostgreSQL (free). Copy the *Internal Database URL*.
3. Render → New → Web Service → connect repo → Runtime **Docker**.
4. Add env vars: `DATABASE_URL`, `JWT_SECRET`, `ADMIN_PASSWORD`.
5. Health check path: `/health`. Deploy, then put the URL and credentials above.

Note: Render's free tier sleeps after inactivity (first request is slow) and the free Postgres expires after about 30 days. Mention this to the reviewer.

## Architecture
```
Browser (React) ──REST──▶ FastAPI ──▶ Postgres
        ▲                    │
        └──── WebSocket ◀────┘  (per-auction rooms, push on every accepted bid)
```
- **Writes go through REST, reads are pushed over WebSocket.** Bids need auth, validation and status codes, which REST handles cleanly. The socket only broadcasts updates.
- **Concurrency-safe bidding.** A bid is a single conditional `UPDATE ... WHERE current_price + min_increment <= :amount AND live`. The database row lock serialises competing bidders, so there are no race conditions and no distributed lock is needed. `rowcount == 0` means rejected.
- **Auction lifecycle is derived, not stored.** Status (upcoming/live/ended) is computed from `starts_at`/`ends_at`, so there is no cron job to fall out of sync. Only `cancelled` is stored.
- **Security:** scrypt password hashing, JWT with expiry, role checks on admin routes, input validation (Pydantic), masked bidder emails, parameterised queries via the ORM.
- **Observability:** JSON structured logs with request IDs, `/health` (checks DB), `/metrics` (Prometheus text format: requests by status, bids accepted/rejected, login failures, WS connections).

## Scaling path
- WebSocket rooms are in-process memory. With multiple instances, publish bid events via Redis pub/sub and have each instance fan out to its own sockets.
- Add a rate limiter (per user and IP) on login and bid endpoints.
- Add read replica or caching for the auction list; paginate it.
- Replace `create_all` with Alembic migrations.

## Assumptions & trade-offs
- Single currency (INR), no payments or KYC; "winner" is the highest bidder when the auction ends.
- No anti-sniping extension and no proxy (auto) bidding, to keep the core bid path simple and correct.
- Plain `Bid` rows are the audit log; the auction row holds the denormalised current price for fast reads.
- The frontend is one file using React from a CDN, to avoid a build step in this timeframe. In production I'd move to Vite + TypeScript with a component structure.
- JWT stored in localStorage for simplicity; httpOnly cookies would be safer against XSS.
- The metrics counters are per-process; use `prometheus_client` for multi-worker setups.
- Tests cover bid rules, permissions, validation and lifecycle states. Not covered yet: WebSocket broadcast and a true concurrent-bid load test.
