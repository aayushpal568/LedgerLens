"""Cloud PostgreSQL data store and database abstraction for LedgerLens.

Provides authoritative PostgreSQL persistence in production using asyncpg connection
pools and real SQL queries (WHERE, ORDER BY, LIMIT, COUNT, transactions).
Eliminates silent in-memory fallback: PostgreSQL mode connects to PostgreSQL or fails clearly.
Provides an explicit MemoryDatabase provider for hermetic local unit testing.
"""
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# Default lease/recovery tuning for durable agent runs (overridable via env at service layer).
DEFAULT_AGENT_LEASE_SECONDS = 120
DEFAULT_AGENT_MAX_ATTEMPTS = 3

# Standard collections / tables
COLLECTIONS = [
    "firm",
    "clients",
    "files",
    "templates",
    "scans",
    "findings",
    "users",
    "agent_threads",
    "agent_messages",
    "agent_runs",
    "agent_run_steps",
    "agent_approvals",
]

IMMUTABLE_FIELDS = {"id", "firm_id", "created_at"}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


SUPPORTED_OPERATORS = {"$ne", "$in", "$exists"}


def _validate_identifier(name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


def _match(doc: dict, filt: dict) -> bool:
    for key, cond in (filt or {}).items():
        val = doc.get(key)
        if isinstance(cond, dict):
            unsupported = set(cond.keys()) - SUPPORTED_OPERATORS
            if unsupported:
                op = sorted(unsupported)[0]
                raise ValueError(f"Unsupported filter operator: {op}")
            if "$ne" in cond and val == cond["$ne"]:
                return False
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$exists" in cond and (val is not None) != cond["$exists"]:
                return False
        else:
            if val != cond:
                return False
    return True



def _project(doc: dict, proj: Optional[dict]) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    if not proj:
        return d
    keep = [k for k, v in proj.items() if v == 1 and k != "_id"]
    if keep:
        return {k: d[k] for k in keep if k in d}
    drop = [k for k, v in proj.items() if v == 0]
    for k in drop:
        d.pop(k, None)
    return d


def _sort(rows: List[dict], sort) -> List[dict]:
    if not sort:
        return rows
    if isinstance(sort, tuple):
        sort = [sort]
    elif isinstance(sort, str):
        sort = [(sort, 1)]
    for key, direction in reversed(sort):
        rows = sorted(rows, key=lambda r: (r.get(key) is None, r.get(key)), reverse=(direction == -1))
    return rows


class _InsertResult:
    def __init__(self, ids: List[str]):
        self.inserted_id = ids[0] if ids else None
        self.inserted_ids = ids


class _UpdateResult:
    def __init__(self, matched: int, modified: int, upserted_id: Optional[str] = None):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id


class _DeleteResult:
    def __init__(self, deleted: int):
        self.deleted_count = deleted


def _build_sql_where(filt: Optional[dict], start_idx: int = 1) -> Tuple[str, List[Any]]:
    """Build parameterized SQL WHERE clause and parameter list for PostgreSQL JSONB."""
    if not filt:
        return "", []

    clauses: List[str] = []
    params: List[Any] = []

    def add_param(val: Any) -> str:
        params.append(val)
        return f"${start_idx + len(params) - 1}"

    for raw_key, cond in filt.items():
        key = _validate_identifier(raw_key)

        if isinstance(cond, dict):
            unsupported = set(cond.keys()) - SUPPORTED_OPERATORS
            if unsupported:
                op = sorted(unsupported)[0]
                raise ValueError(f"Unsupported filter operator: {op}")

        if key == "id":
            if isinstance(cond, dict):
                if "$ne" in cond:
                    p = add_param(str(cond["$ne"]))
                    clauses.append(f"id != {p}")
                elif "$in" in cond:
                    p = add_param([str(x) for x in cond["$in"]])
                    clauses.append(f"id = ANY({p}::text[])")
                elif "$exists" in cond:
                    if cond["$exists"]:
                        clauses.append("id IS NOT NULL")
                    else:
                        clauses.append("id IS NULL")
            else:
                p = add_param(str(cond))
                clauses.append(f"id = {p}")
        else:
            if isinstance(cond, dict):
                if "$ne" in cond:
                    val = cond["$ne"]
                    if isinstance(val, bool):
                        p = add_param(val)
                        clauses.append(f"(doc->'{key}') IS DISTINCT FROM to_jsonb({p}::boolean)")
                    elif isinstance(val, (int, float)):
                        p = add_param(val)
                        clauses.append(f"(doc->'{key}') IS DISTINCT FROM to_jsonb({p}::numeric)")
                    elif val is None:
                        clauses.append(f"(doc ? '{key}' AND doc->'{key}' != 'null'::jsonb)")
                    else:
                        p = add_param(str(val))
                        clauses.append(f"(doc->>'{key}') IS DISTINCT FROM {p}")
                elif "$in" in cond:
                    in_vals = cond["$in"]
                    if all(isinstance(x, str) for x in in_vals):
                        p = add_param(list(in_vals))
                        clauses.append(f"(doc->>'{key}') = ANY({p}::text[])")
                    else:
                        in_clauses = []
                        for item in in_vals:
                            p = add_param(item)
                            in_clauses.append(f"doc->'{key}' = to_jsonb({p})")
                        clauses.append(f"({' OR '.join(in_clauses)})" if in_clauses else "FALSE")
                elif "$exists" in cond:
                    if cond["$exists"]:
                        clauses.append(f"(doc ? '{key}' AND doc->'{key}' != 'null'::jsonb)")
                    else:
                        clauses.append(f"(NOT (doc ? '{key}') OR doc->'{key}' = 'null'::jsonb)")
            else:
                val = cond
                if isinstance(val, bool):
                    p = add_param(val)
                    clauses.append(f"(doc->'{key}') = to_jsonb({p}::boolean)")
                elif isinstance(val, int):
                    p = add_param(val)
                    clauses.append(f"(doc->'{key}') = to_jsonb({p}::bigint)")
                elif isinstance(val, float):
                    p = add_param(val)
                    clauses.append(f"(doc->'{key}') = to_jsonb({p}::numeric)")
                elif val is None:
                    clauses.append(f"(NOT (doc ? '{key}') OR doc->'{key}' = 'null'::jsonb)")
                elif isinstance(val, (dict, list)):
                    p = add_param(json.dumps(val))
                    clauses.append(f"(doc->'{key}') = {p}::jsonb")
                else:
                    p = add_param(str(val))
                    clauses.append(f"(doc->>'{key}') = {p}")

    where_str = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_str, params


def _build_sql_sort(sort) -> str:
    """Build SQL ORDER BY clause for PostgreSQL queries."""
    if not sort:
        return ""
    if isinstance(sort, str):
        sort = [(sort, 1)]
    elif isinstance(sort, tuple) and len(sort) == 2 and not isinstance(sort[0], tuple):
        sort = [sort]

    order_clauses = []
    for raw_field, direction in sort:
        field = _validate_identifier(raw_field)
        dir_str = "DESC" if direction == -1 else "ASC"
        nulls_str = "NULLS LAST" if direction == 1 else "NULLS FIRST"
        if field == "id":
            order_clauses.append(f"id {dir_str}")
        elif field in ("confidence", "size", "progress", "processed_files", "total_files", "total_findings", "expected_period"):
            order_clauses.append(f"COALESCE((doc->>'{field}')::numeric, 0) {dir_str} {nulls_str}")
        else:
            order_clauses.append(f"(doc->>'{field}') {dir_str} {nulls_str}")

    return " ORDER BY " + ", ".join(order_clauses) if order_clauses else ""


# ---------------------------------------------------------------------------
# PostgreSQL Implementation
# ---------------------------------------------------------------------------

class PostgresCursor:
    """Async cursor for PostgreSQL queries supporting sorting, limits, and projection."""

    def __init__(self, collection: "PostgresCollection", filt: Optional[dict], proj: Optional[dict]):
        self._c = collection
        self._filt = filt or {}
        self._proj = proj
        self._sort = None

    def sort(self, key, direction=1):
        self._sort = (key, direction) if not isinstance(key, list) else key
        return self

    async def to_list(self, length: Optional[int] = None) -> List[dict]:
        where_sql, params = _build_sql_where(self._filt)
        order_sql = _build_sql_sort(self._sort)
        limit_sql = f" LIMIT {int(length)}" if length is not None else ""
        query = f"SELECT doc FROM {self._c.name}{where_sql}{order_sql}{limit_sql}"

        async with self._c._db.pool.acquire() as conn:
            records = await conn.fetch(query, *params)

        results = []
        for r in records:
            doc = json.loads(r["doc"]) if isinstance(r["doc"], str) else dict(r["doc"])
            results.append(_project(doc, self._proj))
        return results


class PostgresCollection:
    """PostgreSQL table abstraction with collection-style interface."""

    def __init__(self, db: "PostgresDatabase", name: str):
        self._db = db
        self.name = _validate_identifier(name)

    async def find_one(self, filt: Optional[dict] = None, projection: Optional[dict] = None, sort=None) -> Optional[dict]:
        if not filt:
            raise ValueError("find_one requires a non-empty filter to prevent returning an arbitrary document.")
        where_sql, params = _build_sql_where(filt)
        order_sql = _build_sql_sort(sort)
        query = f"SELECT doc FROM {self.name}{where_sql}{order_sql} LIMIT 1"

        async with self._db.pool.acquire() as conn:
            record = await conn.fetchrow(query, *params)

        if not record:
            return None
        doc = json.loads(record["doc"]) if isinstance(record["doc"], str) else dict(record["doc"])
        return _project(doc, projection)

    def find(self, filt: Optional[dict] = None, projection: Optional[dict] = None) -> PostgresCursor:
        return PostgresCursor(self, filt, projection)

    async def count_documents(self, filt: Optional[dict] = None) -> int:
        where_sql, params = _build_sql_where(filt or {})
        query = f"SELECT COUNT(*) FROM {self.name}{where_sql}"

        async with self._db.pool.acquire() as conn:
            val = await conn.fetchval(query, *params)
        return int(val or 0)

    async def insert_one(self, doc: dict, upsert: bool = True) -> _InsertResult:
        d = dict(doc)
        d.pop("_id", None)
        if "id" not in d:
            d["id"] = str(uuid.uuid4())
        doc_id = str(d["id"])
        doc_json = json.dumps(d)
        if not upsert or self.name == "users":
            query = f"""
                INSERT INTO {self.name} (id, doc, created_at, updated_at)
                VALUES ($1, $2::jsonb, NOW(), NOW())
            """
        else:
            query = f"""
                INSERT INTO {self.name} (id, doc, created_at, updated_at)
                VALUES ($1, $2::jsonb, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()
            """
        async with self._db.pool.acquire() as conn:
            await conn.execute(query, doc_id, doc_json)
        return _InsertResult([doc_id])

    async def insert_many(self, docs: List[dict]) -> _InsertResult:
        if not docs:
            return _InsertResult([])
        ids: List[str] = []
        rows: List[Tuple[str, str]] = []
        for doc in docs:
            d = dict(doc)
            d.pop("_id", None)
            doc_id = str(d["id"])
            ids.append(doc_id)
            rows.append((doc_id, json.dumps(d)))

        query = f"""
            INSERT INTO {self.name} (id, doc, created_at, updated_at)
            VALUES ($1, $2::jsonb, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()
        """
        async with self._db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(query, rows)
        return _InsertResult(ids)

    async def update_one(self, filt: dict, update: dict, upsert: bool = False) -> _UpdateResult:
        where_sql, params = _build_sql_where(filt)
        # Strip immutable fields from $set to protect tenant/system integrity
        set_fields = {k: v for k, v in update.get("$set", {}).items() if k not in IMMUTABLE_FIELDS}
        async with self._db.pool.acquire() as conn:
            async with conn.transaction():
                query = f"SELECT id, doc FROM {self.name}{where_sql} LIMIT 1 FOR UPDATE"
                record = await conn.fetchrow(query, *params)
                if record is not None:
                    doc_id = record["id"]
                    current_doc = json.loads(record["doc"]) if isinstance(record["doc"], str) else dict(record["doc"])
                    current_doc.update(set_fields)
                    await conn.execute(
                        f"UPDATE {self.name} SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                        json.dumps(current_doc), doc_id,
                    )
                    return _UpdateResult(1, 1)

                if upsert:
                    new_doc = {k: v for k, v in filt.items() if not isinstance(v, dict)}
                    new_doc.update(set_fields)
                    if "id" not in new_doc:
                        new_doc["id"] = str(uuid.uuid4())
                    doc_id = str(new_doc["id"])
                    await conn.execute(
                        f"""INSERT INTO {self.name} (id, doc, created_at, updated_at)
                            VALUES ($1, $2::jsonb, NOW(), NOW())
                            ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()""",
                        doc_id, json.dumps(new_doc),
                    )
                    return _UpdateResult(0, 0, upserted_id=doc_id)

                return _UpdateResult(0, 0)

    async def delete_one(self, filt: dict) -> _DeleteResult:
        if not filt:
            raise ValueError("delete_one requires a non-empty filter to prevent deleting an arbitrary document.")
        where_sql, params = _build_sql_where(filt)
        query = f"""
            DELETE FROM {self.name}
            WHERE id = (SELECT id FROM {self.name}{where_sql} LIMIT 1)
            RETURNING id
        """
        async with self._db.pool.acquire() as conn:
            deleted = await conn.fetchrow(query, *params)
        return _DeleteResult(1 if deleted else 0)

    async def delete_many(self, filt: dict) -> _DeleteResult:
        where_sql, params = _build_sql_where(filt)
        query = f"DELETE FROM {self.name}{where_sql}"
        async with self._db.pool.acquire() as conn:
            status = await conn.execute(query, *params)
        try:
            count = int(status.split()[-1])
        except (ValueError, IndexError):
            count = 0
        return _DeleteResult(count)


class PostgresDatabase:
    """PostgreSQL-backed authoritative database manager.

    Stores accounting records as JSONB documents in partitioned PostgreSQL tables,
    with an async connection pool. Does NOT fall back to in-memory storage:
    if PostgreSQL is unreachable or fails, errors are raised clearly.
    """

    def __init__(self, database_url: str):
        if not database_url or not str(database_url).strip():
            raise ValueError(
                "DATABASE_URL is required when PostgreSQL backend is configured. "
                "For test environments without PostgreSQL, explicitly set DATA_BACKEND=memory."
            )
        self.database_url = str(database_url).strip()
        self._collections: Dict[str, PostgresCollection] = {}
        self._pg_pool = None

    @property
    def pool(self):
        if self._pg_pool is None:
            raise RuntimeError(
                "PostgreSQL connection pool is not initialized. Call 'await db.init()' before querying."
            )
        return self._pg_pool

    async def init(self):
        """Initialize PostgreSQL connection pool and ensure required schema exists.

        Fails clearly if PostgreSQL cannot be reached. Never falls back to memory.
        """
        import asyncpg
        safe_url = self.database_url.split("@")[-1] if "@" in self.database_url else self.database_url
        try:
            self._pg_pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=20)
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL at {safe_url}: {e}")
            raise RuntimeError(f"PostgreSQL connection failed: {e}") from e

        try:
            async with self._pg_pool.acquire() as conn:
                # Protect concurrent startup DDL from multi-worker catalog deadlocks
                await conn.execute("SELECT pg_advisory_lock(7483921);")
                try:
                    for col in COLLECTIONS:
                        await conn.execute(f"""
                            CREATE TABLE IF NOT EXISTS {col} (
                                id TEXT PRIMARY KEY,
                                doc JSONB NOT NULL,
                                created_at TIMESTAMPTZ DEFAULT NOW(),
                                updated_at TIMESTAMPTZ DEFAULT NOW()
                            );
                            CREATE INDEX IF NOT EXISTS idx_{col}_doc ON {col} USING GIN (doc);
                        """)
                    # Tenant isolation and unique email indexes
                    await conn.execute("""
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users ((lower(doc->>'email')));
                        CREATE INDEX IF NOT EXISTS idx_clients_firm ON clients ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_files_firm ON files ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_templates_firm ON templates ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_scans_firm ON scans ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_findings_firm ON findings ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_files_client ON files ((doc->>'client_id'));
                        CREATE INDEX IF NOT EXISTS idx_scans_client ON scans ((doc->>'client_id'));
                        CREATE INDEX IF NOT EXISTS idx_findings_scan ON findings ((doc->>'scan_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_threads_firm ON agent_threads ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_messages_firm ON agent_messages ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_runs_firm ON agent_runs ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_run_steps_firm ON agent_run_steps ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_messages_thread ON agent_messages ((doc->>'thread_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_runs_thread ON agent_runs ((doc->>'thread_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run ON agent_run_steps ((doc->>'run_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_approvals_firm ON agent_approvals ((doc->>'firm_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_approvals_run ON agent_approvals ((doc->>'run_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_approvals_status ON agent_approvals ((doc->>'status'));
                        CREATE INDEX IF NOT EXISTS idx_agent_approvals_thread ON agent_approvals ((doc->>'thread_id'));
                        CREATE INDEX IF NOT EXISTS idx_agent_runs_idempotency ON agent_runs ((doc->>'idempotency_key'));
                        -- Agent-critical hot-path indexes (durable worker queue drain + recovery + approval lookup).
                        CREATE INDEX IF NOT EXISTS idx_agent_runs_queued
                            ON agent_runs (((doc->>'created_at')))
                            WHERE (doc->>'status') = 'queued';
                        CREATE INDEX IF NOT EXISTS idx_agent_runs_running_lease
                            ON agent_runs (((doc->>'lease_expires_at')))
                            WHERE (doc->>'status') = 'running';
                        CREATE INDEX IF NOT EXISTS idx_agent_approvals_pending_run
                            ON agent_approvals ((doc->>'run_id'))
                            WHERE (doc->>'status') = 'pending';
                    """)
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(7483921);")
            logger.info(f"Connected to PostgreSQL database at {safe_url}")
        except Exception as e:
            logger.error(f"Failed to initialize PostgreSQL schema: {e}")
            raise RuntimeError(f"PostgreSQL schema initialization failed: {e}") from e

    async def init_pg(self):
        """Backward-compatible alias for init()."""
        await self.init()

    async def close(self):
        """Closes the asyncpg connection pool."""
        if self._pg_pool is not None:
            await self._pg_pool.close()
            self._pg_pool = None

    async def create_firm_and_user_atomic(self, firm_doc: dict, user_doc: dict, template_docs: List[dict]):
        """Atomically create firm, user, and initial templates. Rejects duplicate emails with ValueError."""
        email_clean = str(user_doc.get("email", "")).strip().lower()
        if not email_clean:
            raise ValueError("Email cannot be empty")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT id FROM users WHERE lower(doc->>'email') = $1 LIMIT 1",
                    email_clean
                )
                if existing:
                    raise ValueError(f"A user with email '{email_clean}' already exists.")

                # Insert firm
                f = dict(firm_doc)
                f.pop("_id", None)
                await conn.execute(
                    "INSERT INTO firm (id, doc, created_at, updated_at) VALUES ($1, $2::jsonb, NOW(), NOW())",
                    str(f["id"]), json.dumps(f)
                )

                # Insert user (strict insert, NO on conflict update)
                u = dict(user_doc)
                u.pop("_id", None)
                u["email"] = email_clean
                await conn.execute(
                    "INSERT INTO users (id, doc, created_at, updated_at) VALUES ($1, $2::jsonb, NOW(), NOW())",
                    str(u["id"]), json.dumps(u)
                )

                # Insert firm's initial checklist templates
                for tpl in template_docs:
                    t = dict(tpl)
                    t.pop("_id", None)
                    await conn.execute(
                        "INSERT INTO templates (id, doc, created_at, updated_at) VALUES ($1, $2::jsonb, NOW(), NOW())",
                        str(t["id"]), json.dumps(t)
                    )

    async def backfill_legacy_firm(self, default_firm_id: str):
        """Safely backfill any legacy ownerless records into default_firm_id."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for col in ["clients", "files", "templates", "scans", "findings"]:
                    await conn.execute(f"""
                        UPDATE {col}
                        SET doc = jsonb_set(doc, '{{firm_id}}', to_jsonb($1::text), true)
                        WHERE (doc->>'firm_id') IS NULL OR (doc->>'firm_id') = ''
                    """, str(default_firm_id))

    async def finalize_scan_atomic(self, scan_id: str, scan_update: dict, finding_docs: List[dict]):
        """Persist findings and update scan status in a single atomic transaction."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if finding_docs:
                    rows = []
                    for doc in finding_docs:
                        d = dict(doc)
                        d.pop("_id", None)
                        rows.append((str(d["id"]), json.dumps(d)))
                    query = """
                        INSERT INTO findings (id, doc, created_at, updated_at)
                        VALUES ($1, $2::jsonb, NOW(), NOW())
                        ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()
                    """
                    await conn.executemany(query, rows)

                row = await conn.fetchrow("SELECT id, doc FROM scans WHERE id = $1 FOR UPDATE", scan_id)
                if row is not None:
                    current_doc = json.loads(row["doc"]) if isinstance(row["doc"], str) else dict(row["doc"])
                    current_doc.update(scan_update)
                    await conn.execute(
                        "UPDATE scans SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                        json.dumps(current_doc), scan_id,
                    )

    async def delete_client_cascade(self, client_id: str, firm_id: Optional[str] = None):
        """Atomically delete client, files, scans, and findings, removing storage objects."""
        import storage
        async with self.pool.acquire() as conn:
            # Query file storage paths before deletion
            if firm_id:
                f_rows = await conn.fetch(
                    "SELECT doc->>'storage_path' as sp FROM files WHERE (doc->>'client_id') = $1 AND (doc->>'firm_id') = $2",
                    client_id, str(firm_id)
                )
            else:
                f_rows = await conn.fetch(
                    "SELECT doc->>'storage_path' as sp FROM files WHERE (doc->>'client_id') = $1",
                    client_id
                )
            for r in f_rows:
                sp = r["sp"]
                if sp:
                    try:
                        storage.delete_object(sp)
                    except Exception as e:
                        logger.warning(f"Error removing object {sp} during cascade delete: {e}")

            async with conn.transaction():
                if firm_id:
                    await conn.execute("""
                        DELETE FROM findings
                        WHERE (doc->>'scan_id') IN (
                            SELECT id FROM scans WHERE (doc->>'client_id') = $1 AND (doc->>'firm_id') = $2
                        )
                    """, client_id, str(firm_id))
                    await conn.execute("DELETE FROM scans WHERE (doc->>'client_id') = $1 AND (doc->>'firm_id') = $2", client_id, str(firm_id))
                    await conn.execute("DELETE FROM files WHERE (doc->>'client_id') = $1 AND (doc->>'firm_id') = $2", client_id, str(firm_id))
                    await conn.execute("DELETE FROM clients WHERE id = $1 AND (doc->>'firm_id') = $2", client_id, str(firm_id))
                else:
                    await conn.execute("""
                        DELETE FROM findings
                        WHERE (doc->>'scan_id') IN (
                            SELECT id FROM scans WHERE (doc->>'client_id') = $1
                        )
                    """, client_id)
                    await conn.execute("DELETE FROM scans WHERE (doc->>'client_id') = $1", client_id)
                    await conn.execute("DELETE FROM files WHERE (doc->>'client_id') = $1", client_id)
                    await conn.execute("DELETE FROM clients WHERE id = $1", client_id)

    @staticmethod
    def _doc_from_row(row) -> dict:
        return json.loads(row["doc"]) if isinstance(row["doc"], str) else dict(row["doc"])

    async def claim_agent_run(
        self,
        run_id: str,
        worker_id: str,
        firm_id: str,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> Optional[dict]:
        """Atomically transition a run to 'running' (compare-and-swap on status/lease).

        A run is claimable only if it is 'queued', or 'running' with an EXPIRED lease
        (dead worker). Enforces firm_id at the SQL layer. Returns the claimed doc or
        None if another worker already holds a live lease or the run is terminal.
        """
        now = _utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, doc FROM agent_runs "
                    "WHERE id = $1 AND (doc->>'firm_id') = $2 FOR UPDATE",
                    str(run_id), str(firm_id),
                )
                if row is None:
                    return None
                doc = self._doc_from_row(row)
                status = doc.get("status")
                lease = _parse_iso(doc.get("lease_expires_at"))
                owned_by_us = (
                    status == "running"
                    and str(doc.get("worker_id")) == str(worker_id)
                    and lease is not None and lease > now
                )
                first_claim = status == "queued"
                reclaim_dead = status == "running" and (lease is None or lease <= now) and not owned_by_us
                if not (first_claim or reclaim_dead or owned_by_us):
                    return None
                if not owned_by_us:
                    doc["attempt"] = int(doc.get("attempt", 0) or 0) + 1
                doc["status"] = "running"
                doc["worker_id"] = str(worker_id)
                doc["started_at"] = doc.get("started_at") or now.isoformat()
                doc["heartbeat_at"] = now.isoformat()
                doc["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                doc["updated_at"] = now.isoformat()
                await conn.execute(
                    "UPDATE agent_runs SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                    json.dumps(doc), str(run_id),
                )
                doc.pop("_id", None)
                return doc

    async def claim_next_queued_agent_run(
        self,
        worker_id: str,
        firm_id: Optional[str] = None,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> Optional[dict]:
        """Atomically pop and claim the oldest queued run (FIFO) using SKIP LOCKED.

        Returns the claimed run doc, or None if the queue is empty. SKIP LOCKED ensures
        concurrent workers never select/claim the same queued row.
        """
        now = _utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if firm_id is not None:
                    row = await conn.fetchrow(
                        "SELECT id, doc FROM agent_runs "
                        "WHERE (doc->>'status') = 'queued' AND (doc->>'firm_id') = $1 "
                        "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED",
                        str(firm_id),
                    )
                else:
                    row = await conn.fetchrow(
                        "SELECT id, doc FROM agent_runs "
                        "WHERE (doc->>'status') = 'queued' "
                        "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                    )
                if row is None:
                    return None
                # Row is already locked FOR UPDATE by this transaction; claim inline so
                # there is no second nested acquire (which would self-deadlock the pool).
                doc = self._doc_from_row(row)
                doc["status"] = "running"
                doc["worker_id"] = str(worker_id)
                doc["started_at"] = doc.get("started_at") or now.isoformat()
                doc["heartbeat_at"] = now.isoformat()
                doc["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                doc["attempt"] = int(doc.get("attempt", 0) or 0) + 1
                doc["updated_at"] = now.isoformat()
                await conn.execute(
                    "UPDATE agent_runs SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                    json.dumps(doc), str(row["id"]),
                )
                doc.pop("_id", None)
                return doc

    async def renew_agent_run_lease(
        self,
        run_id: str,
        worker_id: str,
        firm_id: str,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> bool:
        """Extend the lease for a run held by worker_id. Returns False if not the holder."""
        now = _utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, doc FROM agent_runs "
                    "WHERE id = $1 AND (doc->>'firm_id') = $2 FOR UPDATE",
                    str(run_id), str(firm_id),
                )
                if row is None:
                    return False
                doc = self._doc_from_row(row)
                if doc.get("status") != "running" or str(doc.get("worker_id")) != str(worker_id):
                    return False
                doc["heartbeat_at"] = now.isoformat()
                doc["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
                doc["updated_at"] = now.isoformat()
                await conn.execute(
                    "UPDATE agent_runs SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                    json.dumps(doc), str(run_id),
                )
                return True

    async def recover_stale_agent_runs(
        self,
        max_attempts: int = DEFAULT_AGENT_MAX_ATTEMPTS,
    ) -> dict:
        """Requeue runs whose lease expired (dead worker) or fail them if attempts exhausted.

        Returns {'requeued': n, 'failed': m}. This is the restart-recovery primitive: a
        freshly started worker calls it to pick up runs orphaned by a prior crash/restart.
        """
        now = _utcnow()
        requeued = 0
        failed = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT id, doc FROM agent_runs "
                    "WHERE (doc->>'status') = 'running' "
                    "FOR UPDATE SKIP LOCKED"
                )
                for row in rows:
                    doc = self._doc_from_row(row)
                    lease = _parse_iso(doc.get("lease_expires_at"))
                    if lease is not None and lease > now:
                        continue  # live lease — another worker owns it
                    attempt = int(doc.get("attempt", 0) or 0)
                    if attempt >= max_attempts:
                        doc["status"] = "failed"
                        doc["error"] = "Execution interrupted after maximum recovery attempts."
                        doc["completed_at"] = now.isoformat()
                        doc["lease_expires_at"] = None
                        doc["worker_id"] = None
                        failed += 1
                    else:
                        doc["status"] = "queued"
                        doc["worker_id"] = None
                        doc["lease_expires_at"] = None
                        doc["heartbeat_at"] = None
                        requeued += 1
                    doc["updated_at"] = now.isoformat()
                    await conn.execute(
                        "UPDATE agent_runs SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                        json.dumps(doc), str(row["id"]),
                    )
        return {"requeued": requeued, "failed": failed}

    async def cas_agent_approval(
        self,
        approval_id: str,
        firm_id: str,
        expect_statuses: List[str],
        to_status: str,
        set_fields: Optional[dict] = None,
    ) -> bool:
        """Atomic compare-and-swap on an approval's status (SELECT ... FOR UPDATE).

        Transitions approval to ``to_status`` ONLY if it currently belongs to ``firm_id``
        and its status is in ``expect_statuses``. Returns True iff this call performed the
        transition — so concurrent approvers/resumes cannot move the same approval twice.
        """
        now = _utcnow()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, doc FROM agent_approvals "
                    "WHERE id = $1 AND (doc->>'firm_id') = $2 FOR UPDATE",
                    str(approval_id), str(firm_id),
                )
                if row is None:
                    return False
                doc = self._doc_from_row(row)
                if doc.get("status") not in expect_statuses:
                    return False
                doc["status"] = to_status
                for k, v in (set_fields or {}).items():
                    doc[k] = v
                doc["updated_at"] = now.isoformat()
                await conn.execute(
                    "UPDATE agent_approvals SET doc = $1::jsonb, updated_at = NOW() WHERE id = $2",
                    json.dumps(doc), str(approval_id),
                )
                return True

    def __getattr__(self, name: str) -> PostgresCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = PostgresCollection(self, name)
        return self._collections[name]



# ---------------------------------------------------------------------------
# In-Memory Implementation (Explicit Test Backend)
# ---------------------------------------------------------------------------

class MemoryCursor:
    """Async cursor for in-memory collections."""

    def __init__(self, collection: "MemoryCollection", filt: dict, proj: Optional[dict]):
        self._c = collection
        self._filt = filt
        self._proj = proj
        self._sort = None

    def sort(self, key, direction=1):
        self._sort = (key, direction) if not isinstance(key, list) else key
        return self

    async def to_list(self, length: Optional[int] = None) -> List[dict]:
        with self._c._db._lock:
            rows = [dict(v) for v in self._c._db._data.get(self._c.name, {}).values() if _match(v, self._filt)]
        rows = _sort(rows, self._sort)
        if length is not None:
            rows = rows[:length]
        return [_project(r, self._proj) for r in rows]


class MemoryCollection:
    """In-memory collection for unit testing without external infrastructure."""

    def __init__(self, db: "MemoryDatabase", name: str):
        self._db = db
        self.name = name

    async def find_one(self, filt: Optional[dict] = None, projection: Optional[dict] = None, sort=None) -> Optional[dict]:
        if not filt:
            raise ValueError("find_one requires a non-empty filter to prevent returning an arbitrary document.")
        with self._db._lock:
            rows = [dict(v) for v in self._db._data.get(self.name, {}).values() if _match(v, filt)]
        rows = _sort(rows, sort)
        return _project(rows[0], projection) if rows else None

    def find(self, filt: Optional[dict] = None, projection: Optional[dict] = None) -> MemoryCursor:
        return MemoryCursor(self, filt or {}, projection)

    async def count_documents(self, filt: Optional[dict] = None) -> int:
        with self._db._lock:
            return sum(1 for v in self._db._data.get(self.name, {}).values() if _match(v, filt or {}))

    async def insert_one(self, doc: dict, upsert: bool = True) -> _InsertResult:
        d = dict(doc)
        d.pop("_id", None)
        if "id" not in d:
            d["id"] = str(uuid.uuid4())
        doc_id = str(d["id"])
        with self._db._lock:
            col = self._db._data.setdefault(self.name, {})
            if self.name == "users":
                email_clean = str(d.get("email", "")).strip().lower()
                for existing in col.values():
                    if str(existing.get("email", "")).strip().lower() == email_clean:
                        raise ValueError(f"A user with email '{email_clean}' already exists.")
            if not upsert and doc_id in col:
                raise ValueError(f"Document with id '{doc_id}' already exists.")
            col[doc_id] = d
        return _InsertResult([doc_id])

    async def insert_many(self, docs: List[dict]) -> _InsertResult:
        if not docs:
            return _InsertResult([])
        ids: List[str] = []
        with self._db._lock:
            col = self._db._data.setdefault(self.name, {})
            backup = dict(col)
            try:
                for doc in docs:
                    d = dict(doc)
                    d.pop("_id", None)
                    doc_id = str(d["id"])
                    col[doc_id] = d
                    ids.append(doc_id)
                return _InsertResult(ids)
            except Exception:
                self._db._data[self.name] = backup
                raise

    async def update_one(self, filt: dict, update: dict, upsert: bool = False) -> _UpdateResult:
        with self._db._lock:
            col = self._db._data.setdefault(self.name, {})
            target = next((v for v in col.values() if _match(v, filt)), None)
            if target is not None:
                # Handle $set
                for k, v in update.get("$set", {}).items():
                    if k in IMMUTABLE_FIELDS:
                        continue
                    if "." in k:
                        parts = k.split(".")
                        curr = target
                        for p in parts[:-1]:
                            if p not in curr or not isinstance(curr[p], dict):
                                curr[p] = {}
                            curr = curr[p]
                        curr[parts[-1]] = v
                    else:
                        target[k] = v

                # Handle $inc
                for k, v in update.get("$inc", {}).items():
                    if "." in k:
                        parts = k.split(".")
                        curr = target
                        for p in parts[:-1]:
                            if p not in curr or not isinstance(curr[p], dict):
                                curr[p] = {}
                            curr = curr[p]
                        curr[parts[-1]] = curr.get(parts[-1], 0) + v
                    else:
                        target[k] = target.get(k, 0) + v

                return _UpdateResult(1, 1)
            if upsert:
                new_doc = {k: v for k, v in filt.items() if not isinstance(v, dict)}
                set_fields = {k: v for k, v in update.get("$set", {}).items() if k not in IMMUTABLE_FIELDS}
                new_doc.update(set_fields)
                if "id" not in new_doc:
                    new_doc["id"] = str(uuid.uuid4())
                doc_id = str(new_doc["id"])
                col[doc_id] = new_doc
                return _UpdateResult(0, 0, upserted_id=doc_id)
            return _UpdateResult(0, 0)

    async def delete_one(self, filt: dict) -> _DeleteResult:
        if not filt:
            raise ValueError("delete_one requires a non-empty filter to prevent deleting an arbitrary document.")
        with self._db._lock:
            col = self._db._data.setdefault(self.name, {})
            target_id = next((k for k, v in col.items() if _match(v, filt)), None)
            if target_id is None:
                return _DeleteResult(0)
            col.pop(target_id, None)
            return _DeleteResult(1)

    async def delete_many(self, filt: dict) -> _DeleteResult:
        with self._db._lock:
            col = self._db._data.setdefault(self.name, {})
            target_ids = [k for k, v in col.items() if _match(v, filt)]
            for tid in target_ids:
                col.pop(tid, None)
            return _DeleteResult(len(target_ids))


class MemoryDatabase:
    """Explicit in-memory database provider for local unit tests and environments
    without external PostgreSQL infrastructure.
    """

    def __init__(self):
        self._collections: Dict[str, MemoryCollection] = {}
        self._data: Dict[str, Dict[str, dict]] = {col: {} for col in COLLECTIONS}
        self._lock = threading.RLock()

    async def init(self):
        pass

    async def init_pg(self):
        pass

    async def close(self):
        with self._lock:
            self._data.clear()

    async def create_firm_and_user_atomic(self, firm_doc: dict, user_doc: dict, template_docs: List[dict]):
        """Atomically create firm, user, and seeded templates in memory."""
        email_clean = str(user_doc.get("email", "")).strip().lower()
        if not email_clean:
            raise ValueError("Email cannot be empty")

        with self._lock:
            users_col = self._data.setdefault("users", {})
            for existing_user in users_col.values():
                if str(existing_user.get("email", "")).strip().lower() == email_clean:
                    raise ValueError(f"A user with email '{email_clean}' already exists.")

            # Create firm
            f = dict(firm_doc)
            f.pop("_id", None)
            self._data.setdefault("firm", {})[str(f["id"])] = f

            # Create user
            u = dict(user_doc)
            u.pop("_id", None)
            u["email"] = email_clean
            users_col[str(u["id"])] = u

            # Create templates
            tpl_col = self._data.setdefault("templates", {})
            for tpl in template_docs:
                t = dict(tpl)
                t.pop("_id", None)
                tpl_col[str(t["id"])] = t

    async def backfill_legacy_firm(self, default_firm_id: str):
        """Safely backfill any legacy ownerless records into default_firm_id."""
        with self._lock:
            for col_name in ["clients", "files", "templates", "scans", "findings"]:
                col = self._data.setdefault(col_name, {})
                for doc in col.values():
                    if not doc.get("firm_id"):
                        doc["firm_id"] = str(default_firm_id)

    async def finalize_scan_atomic(self, scan_id: str, scan_update: dict, finding_docs: List[dict]):
        """Atomically persist findings and update scan status."""
        with self._lock:
            findings_backup = dict(self._data.get("findings", {}))
            scans_backup = dict(self._data.get("scans", {}))
            try:
                col_f = self._data.setdefault("findings", {})
                for doc in finding_docs:
                    d = dict(doc)
                    d.pop("_id", None)
                    col_f[str(d["id"])] = d
                scan = self._data.setdefault("scans", {}).get(scan_id)
                if scan is not None:
                    scan.update(scan_update)
            except Exception:
                self._data["findings"] = findings_backup
                self._data["scans"] = scans_backup
                raise

    async def delete_client_cascade(self, client_id: str, firm_id: Optional[str] = None):
        """Atomically delete client, files, scans, and findings, removing storage objects."""
        import storage
        with self._lock:
            backup = {col: dict(self._data.get(col, {})) for col in COLLECTIONS}
            try:
                # Delete files from object storage
                files_col = self._data.get("files", {})
                for f in files_col.values():
                    if f.get("client_id") == client_id and (not firm_id or f.get("firm_id") == str(firm_id)):
                        sp = f.get("storage_path")
                        if sp:
                            try:
                                storage.delete_object(sp)
                            except Exception as e:
                                logger.warning(f"Error removing object {sp} during cascade: {e}")

                scan_ids = [
                    sid for sid, s in self._data.get("scans", {}).items()
                    if s.get("client_id") == client_id and (not firm_id or s.get("firm_id") == str(firm_id))
                ]
                self._data["findings"] = {
                    fid: f for fid, f in self._data.get("findings", {}).items()
                    if f.get("scan_id") not in scan_ids
                }
                self._data["scans"] = {
                    sid: s for sid, s in self._data.get("scans", {}).items()
                    if not (s.get("client_id") == client_id and (not firm_id or s.get("firm_id") == str(firm_id)))
                }
                self._data["files"] = {
                    fid: f for fid, f in self._data.get("files", {}).items()
                    if not (f.get("client_id") == client_id and (not firm_id or f.get("firm_id") == str(firm_id)))
                }
                clients_col = self._data.get("clients", {})
                if client_id in clients_col:
                    if not firm_id or clients_col[client_id].get("firm_id") == str(firm_id):
                        clients_col.pop(client_id, None)
            except Exception:
                self._data.update(backup)
                raise

    async def claim_agent_run(
        self,
        run_id: str,
        worker_id: str,
        firm_id: str,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> Optional[dict]:
        """Atomic compare-and-swap claim of a single run under the in-memory lock."""
        now = _utcnow()
        with self._lock:
            doc = self._data.setdefault("agent_runs", {}).get(str(run_id))
            if doc is None or str(doc.get("firm_id")) != str(firm_id):
                return None
            status = doc.get("status")
            lease = _parse_iso(doc.get("lease_expires_at"))
            owned_by_us = (
                status == "running"
                and str(doc.get("worker_id")) == str(worker_id)
                and lease is not None and lease > now
            )
            first_claim = status == "queued"
            reclaim_dead = status == "running" and (lease is None or lease <= now) and not owned_by_us
            if not (first_claim or reclaim_dead or owned_by_us):
                return None
            result = dict(doc)
            if not owned_by_us:
                result["attempt"] = int(result.get("attempt", 0) or 0) + 1
            result["status"] = "running"
            result["worker_id"] = str(worker_id)
            result["started_at"] = result.get("started_at") or now.isoformat()
            result["heartbeat_at"] = now.isoformat()
            result["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            result["updated_at"] = now.isoformat()
            self._data["agent_runs"][str(run_id)] = result
            out = dict(result)
            out.pop("_id", None)
            return out

    async def claim_next_queued_agent_run(
        self,
        worker_id: str,
        firm_id: Optional[str] = None,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> Optional[dict]:
        """Atomically pop and claim the oldest queued run (FIFO) under the in-memory lock."""
        now = _utcnow()
        with self._lock:
            col = self._data.setdefault("agent_runs", {})
            candidates = [
                (doc.get("created_at") or "", doc.get("id"))
                for doc in col.values()
                if doc.get("status") == "queued" and (firm_id is None or str(doc.get("firm_id")) == str(firm_id))
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda t: (t[0] or "", str(t[1] or "")))
            run_id = candidates[0][1]
            doc = col.get(str(run_id))
            if doc is None:
                return None
            result = dict(doc)
            result["status"] = "running"
            result["worker_id"] = str(worker_id)
            result["started_at"] = result.get("started_at") or now.isoformat()
            result["heartbeat_at"] = now.isoformat()
            result["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            result["attempt"] = int(result.get("attempt", 0) or 0) + 1
            result["updated_at"] = now.isoformat()
            col[str(run_id)] = result
            out = dict(result)
            out.pop("_id", None)
            return out

    async def renew_agent_run_lease(
        self,
        run_id: str,
        worker_id: str,
        firm_id: str,
        lease_seconds: int = DEFAULT_AGENT_LEASE_SECONDS,
    ) -> bool:
        """Extend the lease for a run held by worker_id; False if not the current holder."""
        now = _utcnow()
        with self._lock:
            doc = self._data.setdefault("agent_runs", {}).get(str(run_id))
            if doc is None or str(doc.get("firm_id")) != str(firm_id):
                return False
            if doc.get("status") != "running" or str(doc.get("worker_id")) != str(worker_id):
                return False
            doc["heartbeat_at"] = now.isoformat()
            doc["lease_expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
            doc["updated_at"] = now.isoformat()
            return True

    async def recover_stale_agent_runs(
        self,
        max_attempts: int = DEFAULT_AGENT_MAX_ATTEMPTS,
    ) -> dict:
        """Requeue or fail running runs whose lease expired (dead worker/crash recovery)."""
        now = _utcnow()
        requeued = 0
        failed = 0
        with self._lock:
            col = self._data.setdefault("agent_runs", {})
            for run_id, doc in list(col.items()):
                if doc.get("status") != "running":
                    continue
                lease = _parse_iso(doc.get("lease_expires_at"))
                if lease is not None and lease > now:
                    continue  # live lease owned by another worker
                attempt = int(doc.get("attempt", 0) or 0)
                doc["lease_expires_at"] = None
                doc["worker_id"] = None
                doc["updated_at"] = now.isoformat()
                if attempt >= max_attempts:
                    doc["status"] = "failed"
                    doc["error"] = "Execution interrupted after maximum recovery attempts."
                    doc["completed_at"] = now.isoformat()
                    failed += 1
                else:
                    doc["status"] = "queued"
                    doc["heartbeat_at"] = None
                    requeued += 1
        return {"requeued": requeued, "failed": failed}

    async def cas_agent_approval(
        self,
        approval_id: str,
        firm_id: str,
        expect_statuses: List[str],
        to_status: str,
        set_fields: Optional[dict] = None,
    ) -> bool:
        """Atomic compare-and-swap on an approval's status under the in-memory lock."""
        now = _utcnow()
        with self._lock:
            col = self._data.setdefault("agent_approvals", {})
            doc = col.get(str(approval_id))
            if doc is None or str(doc.get("firm_id")) != str(firm_id):
                return False
            if doc.get("status") not in expect_statuses:
                return False
            doc["status"] = to_status
            for k, v in (set_fields or {}).items():
                doc[k] = v
            doc["updated_at"] = now.isoformat()
            return True

    def __getattr__(self, name: str) -> MemoryCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = MemoryCollection(self, name)
        return self._collections[name]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_db_instance: Optional[Union[PostgresDatabase, MemoryDatabase]] = None


def get_database(
    database_url: Optional[str] = None,
    backend: Optional[str] = None,
    reset: bool = False,
) -> Union[PostgresDatabase, MemoryDatabase]:
    """Factory to acquire database instance based on backend configuration.

    Modes:
    - 'postgres': authoritative PostgreSQL persistence via DATABASE_URL.
      Fails clearly if DATABASE_URL is missing or connection fails.
    - 'memory': explicit in-memory provider for unit tests.
    """
    global _db_instance
    if _db_instance is not None and not reset:
        return _db_instance

    target_backend = (backend or os.environ.get("DATA_BACKEND", "postgres")).strip().lower()

    if target_backend in ("memory", "test", "inmemory"):
        instance: Union[PostgresDatabase, MemoryDatabase] = MemoryDatabase()
    elif target_backend == "postgres":
        url = database_url if database_url is not None else os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise ValueError(
                "DATABASE_URL is required when PostgreSQL backend is configured. "
                "For test environments without PostgreSQL, explicitly set DATA_BACKEND=memory."
            )
        instance = PostgresDatabase(url)
    else:
        raise ValueError(f"Unsupported DATA_BACKEND: '{target_backend}'. Must be 'postgres' or 'memory'.")

    if not reset:
        _db_instance = instance
    return instance
