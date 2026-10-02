# LedgerLens Cloud

LedgerLens is an AI-powered accounting document verification, checklist auditing, and duplicate detection platform designed for modern accounting and audit firms.

## Architecture Overview

LedgerLens Cloud is engineered for reliable, multi-tenant SaaS deployment:
- **Backend**: FastAPI with async multi-worker safety, advisory locking, strict tenant scoping (`AuthedUser`), and bounded execution limits.
- **Frontend**: React application featuring responsive firm dashboards, client file management, exception review center, and an interactive Claude-powered accounting AI assistant.
- **Database**: Authoritative PostgreSQL with `asyncpg` connection pool, pure SQL filtering, transactional consistency, and advisory lock protection. Also includes an in-memory mode for rapid, hermetic unit tests.
- **Object Storage**: S3/Cloudflare R2-compatible storage via `boto3` offloaded from the async event loop, with fallback to local storage in development.
- **AI & OCR Providers**:
  - **Deterministic Fast Path**: Native digital PDFs, CSVs, Excel workbooks, and DOCX files are extracted locally without external API latency or costs.
  - **PaddleOCR**: Local optical character recognition for scanned receipts, invoices, and image documents running directly on the host machine without external API dependencies or costs.
  - **Claude Opus (fal.ai / Anthropic direct)**: Powers the conversational accounting agent and handles ambiguous document classification fallback with bounded turn budgets and factual grounding checks.

---

## Environment Configuration

Copy `.env.example` to `backend/.env` and configure your credentials:

```bash
# Core Environment
ENVIRONMENT=production
PORT=8000
AUTH_SECRET_KEY=your-secure-random-32-character-secret-key
CORS_ORIGINS=https://app.ledgerlens.com,http://localhost:3000

# Database
DATA_BACKEND=postgres
DATABASE_URL=postgresql://postgres:password@localhost:5432/ledgerlens

# Object Storage (S3 / Cloudflare R2)
STORAGE_BACKEND=s3
S3_BUCKET=ledgerlens-production-documents
AWS_ACCESS_KEY_ID=your-access-key-id
AWS_SECRET_ACCESS_KEY=your-secret-access-key
AWS_REGION=auto
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com

# OCR & AI Services (Optional / As Needed)
# PADDLE_OCR_USE_GPU=false
FAL_KEY=your-fal-ai-api-key
```

---

## Running Verification and Tests

```bash
# Run backend pytest suite (100% hermetic, zero paid API charges)
.\backend\.venv\Scripts\pytest -q

# Run end-to-end backend verification
python backend/verify_all.py

# Run frontend tests
cd frontend && npm test -- --watchAll=false

# Build frontend production bundle
npm run build
```
