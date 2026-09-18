# LedgerLens — Accounting Document Verification Platform (Cloud SaaS)

## Product Vision & Core Mission
LedgerLens Cloud is a multi-tenant SaaS platform for accounting and audit firms to scan client document collections, cross-check requirements against customized checklist templates, and surface actionable exceptions (duplicates, missing records, wrong accounting periods, unsupported file formats).

## Core Guarantees & Non-Negotiables
- **Human-in-the-Loop**: The platform and AI agent never delete, rename, or alter client accounting files; destructive changes are strictly prohibited.
- **Tenant Isolation**: Every database query, storage path, API route, and agent tool execution is strictly scoped by `firm_id` with fail-closed security.
- **Deterministic-First**: Standard digital accounting files (PDFs, Excel spreadsheets, CSVs) are parsed deterministically on the server. External AI/OCR services are only invoked when configured: Baidu Unlimited-OCR for scanned images and Claude Opus for ambiguous document classification and interactive chat.
- **Grounding Safety**: The Claude agent answers questions grounded strictly in validated database facts and tool results, with deterministic checks for entity IDs and finding counts.

## Architecture

### Backend (FastAPI + Python 3.11)
- **Routes & Authentication**: JWT bearer tokens with 15-minute expiry, opaque rolling refresh tokens, bcrypt password hashing, and login rate limiting.
- **PostgreSQL Persistence**: Authoritative transactional database using `asyncpg` connection pool with schema initialization guarded by PostgreSQL advisory locks.
- **Object Storage**: S3/Cloudflare R2-compatible object storage offloaded from the async event loop using `asyncio.to_thread`.
- **Job Reliability**: Orphaned scan and agent run recovery on server startup with cutoff safeguards to prevent killing concurrent active worker jobs.
- **Scan Engine**: Concurrent text extraction, SHA-256 duplicate detection, SequenceMatcher near-duplicate analysis with `autojunk=False`, and checklist matching.

### External Providers
- **OCR**: Baidu Unlimited-OCR (OpenAI-compatible vision endpoint) for image-based receipts, invoices, and scanned documents.
- **LLM**: Claude Opus via fal.ai (with optional Anthropic direct fallback) for the conversational accounting agent and ambiguous document categorization.
- **Extractors**: Local server-side extractors for PDF (PyMuPDF), Word (.docx), Excel (.xlsx), and plain text / CSV.

### Frontend (React + Tailwind CSS)
- Three-pane accounting workspace: Dashboard, Clients, Scan Workspace, Checklist Templates, Review Center, File Compare, Reports, and Agent AI Chat.
- Resilient session management: Transparent token refresh across 15-minute token expiry and page reload (F5).
- Agent AI Chat: ChatGPT-style interface with human approval cards for privileged actions, recent conversation thread persistence, polling backoff/timeout, and deep links to findings in the Review Center.

## Completed Hardening & Fixes
- Near-duplicate SequenceMatcher autojunk issue resolved with regression ledger tests.
- Separated Claude token budgets: 384 tokens for turn decisions and 2048 tokens for final answers.
- Single-retry JSON protocol correction on malformed agent responses.
- Canonical findings schema with file IDs, filenames, evidence, confidence, and notes.
- True magic byte and MIME header validation rejecting disguised binary executables (MZ, ELF, Mach-O).
- Startup PostgreSQL advisory locks and multi-worker cutoff safe orphan recovery.
- Cross-tenant IDOR validation across all API routes and agent action tools.
- Rate limiting for uploads, scans, and messages, plus per-firm AI quota controls.
- Secure password reset flow with 1-hour expiration and session revocation.
