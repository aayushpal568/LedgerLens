# LedgerLens

LedgerLens is an AI-powered accounting document verification and duplicate detection platform.

## Database Configuration

LedgerLens supports two database modes via the `DATA_BACKEND` environment variable:

### 1. PostgreSQL Mode (Production & Cloud Deployment)
**PostgreSQL is required for persistent cloud deployment.** In this mode, LedgerLens uses an authoritative `asyncpg` connection pool with pure SQL filtering, transactional safety, and persistent table storage for firm records, clients, files, templates, scans, and findings.

Configuration (`backend/.env`):
```bash
DATA_BACKEND=postgres
DATABASE_URL=postgresql://<username>:<password>@<host>:<port>/<dbname>
```
*Note: If `DATA_BACKEND=postgres` is set but `DATABASE_URL` is missing or the database is unreachable, the server will fail clearly on startup rather than falling back to in-memory storage.*

### 2. In-Memory Mode (Local Unit Testing)
For hermetic local unit testing and development without external PostgreSQL infrastructure:
```bash
DATA_BACKEND=memory
```
*Note: In-memory mode stores data in memory for the duration of the process. Data is not persistent across restarts.*

## Running Tests and Verification

```bash
# Run automated pytest suite
.\.venv\Scripts\python.exe -m pytest -q

# Run full system verification suite
.\.venv\Scripts\python.exe verify_all.py
```
