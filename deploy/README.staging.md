# LedgerLens V1 — Staging Runbook (PRIMARY: managed PostgreSQL + Cloudflare R2)

Target architecture (this is the default path in `docker-compose.staging.yml`):
- **backend**: ONE container (uvicorn single worker; durable agent worker in-process). Internal only.
- **frontend**: nginx container — serves the built SPA and reverse-proxies `/api` + `/healthz` -> backend (same origin). **The only public entry.**
- **PostgreSQL**: **managed**, external (`DATABASE_URL`).
- **Object storage**: **Cloudflare R2** (S3-compatible), external.
- **TLS/HTTPS + hostname**: the deployment platform's edge → forwards to `frontend:80`.

Secrets are never committed: `deploy/staging.env` (gitignored `*.env`) is created from
`deploy/staging.env.example`. No real credentials belong in the repo.

---

## 1) Resources to create (external, manual)
1. **Managed PostgreSQL** instance + database + role.
2. **Cloudflare R2** private bucket + an **R2 API token** (S3-compatible Access Key ID/Secret).
3. **Domain + TLS** on your host (e.g. Cloudflare/Let's Encrypt) with a hostname for staging.
4. Compute host/PaaS that can run `docker compose` and reach the above.
5. (Optional) A container **registry** to store versioned images for rollback.
6. (Optional, to exercise agent) **fal.ai/OpenRouter** key (PaddleOCR runs locally in-process).

## 2) Values to obtain from each resource
- Managed PG: `host`, `port` (5432), `database`, `user`, `password`, and confirm SSL.
- R2: `bucket name`, `R2 account id` (for the endpoint), API token `Access Key ID` + `Secret Access Key`.
- Edge/domain: the **public staging origin**, e.g. `https://staging.example.com`.
- Auth: a freshly generated random secret ≥32 chars.
- Claude (optional): your **fal.ai key**. OCR (optional): **endpoint** + **key**.

## 3) Where each value goes → `deploy/staging.env`
`cp deploy/staging.env.example deploy/staging.env` then fill (values illustrative; keep in the secret store):

| Set in `deploy/staging.env`        | Value / source |
|------------------------------------|----------------|
| `ENVIRONMENT=production`           | fixed (activates fail-closed guards) |
| `DATA_BACKEND=postgres`            | fixed |
| `DATABASE_URL`                     | `postgresql://<PG_USER>:<PG_PASSWORD>@<PG_HOST>:5432/<PG_DATABASE>?sslmode=require` |
| `AUTH_SECRET_KEY`                  | your random ≥32-char secret |
| `CORS_ORIGINS`                     | `https://staging.example.com` (the public origin) |
| `STORAGE_BACKEND`                  | `s3` |
| `S3_BUCKET`                        | R2 bucket name |
| `AWS_ACCESS_KEY_ID`                | R2 API token Access Key ID |
| `AWS_SECRET_ACCESS_KEY`            | R2 API token Secret |
| `S3_REGION`                        | `auto` |
| `S3_ENDPOINT_URL`                  | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `TRUST_PROXY`                      | `true` (behind the edge; compose also sets it) |
| `FAL_KEY` (optional)               | fal.ai/OpenRouter key |
| `CLAUDE_MODEL` (optional)          | `anthropic/claude-opus-4.6` (default) |
| `PADDLE_OCR_USE_GPU` (optional)    | `false` (default) or `true` for GPU |

Notes: R2 bucket stays **private** (the app streams objects by key server-side; no public URL, no CORS on the bucket). Do not set `EMERGENT_LLM_KEY`/`INTEGRATION_PROXY_URL` for the R2 path. The compose file already injects `HOST/PORT/DATA_BACKEND/TRUST_PROXY/AGENT_WORKER_ENABLED`; anything you also put in `staging.env` overrides.

## 4) Deploy (only after §1–§3 exist)
```bash
# a) Build the SPA bundle (bakes the public API origin into the client):
cd frontend
npm ci
CI=false REACT_APP_BACKEND_URL=https://staging.example.com npx craco build
cd ..

# b) Validate config (no deploy):
docker compose --env-file deploy/staging.env -f docker-compose.staging.yml config >/dev/null && echo "compose OK"

# c) Build + start (managed PG + R2 are external; no db service):
docker compose --env-file deploy/staging.env -f docker-compose.staging.yml -p ledgerlens-staging up -d --build

# d) Watch:
docker compose -p ledgerlens-staging ps
docker compose -p ledgerlens-staging logs -f --tail=80 backend    # expect: "Durable agent worker started."
```
Point the platform edge/TLS hostname at the frontend's published port (`8080:80` → `https://staging.example.com`).

## 5) Smoke test (against the public origin; exercises the nginx proxy too)
```bash
API=https://staging.example.com
curl -fsS "$API/healthz"                    # -> {"status":"healthy","database":"connected"}

# one-time signup, capture token:
RESP=$(curl -fsS -X POST "$API/api/auth/signup" -H 'Content-Type: application/json' \
  -d '{"email":"smoke@example.com","password":"StagingPass123!","firm_name":"Smoke Firm","name":"Smoke"}')
echo "$RESP" | python -c 'import sys,json;print("signed up firm",json.load(sys.stdin)["firm"]["id"])'
export TOKEN=$(echo "$RESP" | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
H="Authorization: Bearer $TOKEN"

CID=$(curl -fsS -X POST "$API/api/clients" -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Smoke Co","client_type":"Small Business"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
printf 'Account,Debit,Credit,Year\n1000,500,0,2024\n1000,0,500,2024\n' > /tmp/a.csv
printf 'Account,Debit,Credit,Year\n1000,500,0,2024\n1000,0,500,2024\n' > /tmp/b.csv
curl -fsS -X POST "$API/api/clients/$CID/files" -H "$H" -F 'files=@/tmp/a.csv;type=text/csv' -F 'files=@/tmp/b.csv;type=text/csv'
SID=$(curl -fsS -X POST "$API/api/clients/$CID/scan" -H "$H" -H 'Content-Type: application/json' \
  -d '{"expected_period":2024}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
sleep 3
curl -fsS "$API/api/scans/$SID" -H "$H" | python -c 'import sys,json;d=json.load(sys.stdin);print("scan:",d["status"],"findings:",d.get("total_findings"))'
curl -fsS "$API/api/scans/$SID/findings" -H "$H" | python -c 'import sys,json;print("cats:",sorted({f["category"] for f in json.load(sys.stdin)}))'
curl -fsS -o /tmp/report.csv "$API/api/scans/$SID/report?format=csv" -H "$H" && head -2 /tmp/report.csv

# AI assistant (needs FAL_KEY): send + poll to completed
RID=$(curl -fsS -X POST "$API/api/agent/messages" -H "$H" -H 'Content-Type: application/json' \
  -d '{"text":"List my clients"}' | python -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
for i in $(seq 1 30); do st=$(curl -fsS "$API/api/agent/runs/$RID" -H "$H" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])'); echo "run=$st"; [ "$st" = completed -o "$st" = failed -o "$st" = waiting_for_approval ] && break; sleep 1; done

# Security spot-checks (expect 401 unauth, 404 cross-tenant):
curl -s -o /dev/null -w "unauth /api/clients -> %{http_code}\n" "$API/api/clients"
```

## 6) Rollback / stop
```bash
# 1. Tag a known-good release IMMEDIATELY after a verified build:
docker tag ledgerlens-v1-staging:latest          ledgerlens-v1-staging:<gitsha>
docker tag ledgerlens-v1-staging-frontend:latest ledgerlens-v1-staging-frontend:<gitsha>

# 2. Stop (managed PG + R2 are external, so nothing is lost):
docker compose -p ledgerlens-staging down

# 3. ROLL BACK = re-point a prior good release to :latest (compose uses :latest), then start:
docker tag ledgerlens-v1-staging:<prev-sha>          ledgerlens-v1-staging:latest
docker tag ledgerlens-v1-staging-frontend:<prev-sha> ledgerlens-v1-staging-frontend:latest
docker compose --env-file deploy/staging.env -f docker-compose.staging.yml -p ledgerlens-staging up -d   # no --build => runs :latest

# 4. ROLL FORWARD after a code change (rebuilds :latest from source):
docker compose --env-file deploy/staging.env -f docker-compose.staging.yml -p ledgerlens-staging up -d --build
```
Because the compose references `:latest`, a rollback is a re-tag + `up -d` (NOT an auto tag swap).
Managed PG data and R2 objects are external and unaffected by `down` (no `pgdata` volume in this
primary manifest). Database backups/snapshots are your managed provider’s responsibility.

## 7) Monthly cost categories (staging)
- Compute (1 host/container): ~$5–20
- Managed PostgreSQL: ~$15–50
- Cloudflare R2: ~$0–5 (storage + **$0 egress**)
- Static hosting / CDN + TLS + domain: ~$0–20
- Claude (fal/OpenRouter): pay-per-token, ~$0–50 (usage-driven; degrades safely if unset)
- PaddleOCR: local in-process, $0 (free, no API cost)
- CI/CD + secrets + monitoring: ~$0–25
Indicative total ≈ **$25–140 / mo + usage**.

## Technical risks to close before/at deploy
1. **SSL mode on `DATABASE_URL`:** managed PG often requires `?sslmode=require`; verify the exact param the provider expects, or `db.init()` fails at startup.
2. **R2 region/endpoint:** wrong `S3_ENDPOINT_URL`/`S3_REGION` => uploads 500; smoke-test a real upload before declaring staging healthy (storage fails closed). Keep bucket private (signed access via keys, not public).
3. **Startup DDL permissions:** the connected role needs `CREATE` (tables + agent indexes). If your provider forbids DDL to the app role, pre-provision the schema once.
4. **Single event loop assumption:** one backend container keeps the asyncpg pool + in-process durable worker correct. If you later scale to >1 replica, the atomic DB claims stay safe, but confirm the platform runs the worker as intended (only one drainer is needed).
5. **Claude/OCR keys:** without them the agent returns a safe "provider unavailable" failure and scanned docs get no OCR — fine for smoke, but flag if the feature demo requires real answers.
6. **Edge trust / headers:** set `TRUST_PROXY=true` and ensure the LB sends `X-Forwarded-For`/`X-Forwarded-Proto` (login throttling + secure cookies depend on it).
7. **requirements.txt pinning:** partially unpinned (`>=`); pin exact versions for byte-reproducible images (non-blocking).
8. **Secrets hygiene:** `deploy/staging.env` is gitignored (`*.env`) and excluded from Docker build context — keep it that way; supply values via the platform secret store where possible.

---

## Appendix — self-hosted PostgreSQL variant (only if not using managed PG)
Add to `docker-compose.staging.yml`:
```yaml
  postgres:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ledgerlens_staging
      POSTGRES_USER: ledgerlens
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}   # put in deploy/staging.env
    volumes: [ pgdata:/var/lib/postgresql/data ]
    healthcheck:
      test: ["CMD-SHELL","pg_isready -U ledgerlens -d ledgerlens_staging"]
      interval: 10s; timeout: 5s; retries: 5
volumes: { pgdata: }
```
On `backend` add: `depends_on: { postgres: { condition: service_healthy } }`, and set
`DATABASE_URL=postgresql://ledgerlens:<PG_PASSWORD>@postgres:5432/ledgerlens_staging`.
This variant keeps data on the `pgdata` volume (survives `down`, destroyed by `down -v`).
