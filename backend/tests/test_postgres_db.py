"""Tests for LedgerLens PostgreSQL persistence layer and database abstractions.

Verifies:
A. Real PostgreSQL create and retrieve client (cleanly skipped if PostgreSQL unavailable)
B. Data persists across repository reinitialization (cleanly skipped if PostgreSQL unavailable)
C. PostgreSQL connection failure raises error and NEVER silently falls back to memory
D. Missing DATABASE_URL in postgres mode raises clear configuration error
E. Explicit in-memory database provider is available for unit tests without external infrastructure
F. Repository client/file/scan/finding operations continue working
G. SQL query and sorting filters build correct parameterized PostgreSQL statements
H. SQL placeholder index bug fix: {"field": {"$ne": None}} never increments parameter index
I. Fail-closed behavior on unsupported filter operators ($gt, $regex, etc.) for Postgres & Memory
J. Empty filter protection: find_one({}) and delete_one({}) raise ValueError
K. Atomic insert_many rollback and atomic cascade / scan finalization
"""
import asyncio
import os
import sys
import uuid
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import (
    PostgresDatabase,
    MemoryDatabase,
    get_database,
    _build_sql_where,
    _build_sql_sort,
    _match,
)


async def _can_connect_postgres(url: str) -> bool:
    """Check if real PostgreSQL server is reachable."""
    if not url:
        return False
    try:
        import asyncpg
        conn = await asyncio.wait_for(asyncpg.connect(url), timeout=2.0)
        await conn.close()
        return True
    except Exception:
        return False


# ===========================================================================
# Requirement G & 1: SQL Query and Sort Builder + Placeholder Bug Fix
# ===========================================================================

def test_sql_where_builder_exact_and_operators():
    # Direct equality
    where, params = _build_sql_where({"client_id": "client-123"})
    assert where == " WHERE (doc->>'client_id') = $1"
    assert params == ["client-123"]

    # Special handling for id column
    where, params = _build_sql_where({"id": "id-abc"})
    assert where == " WHERE id = $1"
    assert params == ["id-abc"]

    # $ne operator with boolean (uses DISTINCT FROM)
    where, params = _build_sql_where({"is_deleted": {"$ne": True}})
    assert "(doc->'is_deleted') IS DISTINCT FROM to_jsonb($1::boolean)" in where
    assert params == [True]

    # $ne operator with number
    where, params = _build_sql_where({"confidence": {"$ne": 50}})
    assert "(doc->'confidence') IS DISTINCT FROM to_jsonb($1::numeric)" in where
    assert params == [50]

    # $in operator with strings
    where, params = _build_sql_where({"category": {"$in": ["unreadable", "mismatch"]}})
    assert "(doc->>'category') = ANY($1::text[])" in where
    assert params == [["unreadable", "mismatch"]]

    # $exists operator (True and False)
    where, params = _build_sql_where({"status": {"$exists": True}})
    assert "(doc ? 'status' AND doc->'status' != 'null'::jsonb)" in where
    assert params == []

    where, params = _build_sql_where({"status": {"$exists": False}})
    assert "(NOT (doc ? 'status') OR doc->'status' = 'null'::jsonb)" in where
    assert params == []

    # Combined clauses
    where, params = _build_sql_where({
        "client_id": "c-1",
        "is_deleted": {"$ne": True},
    })
    assert "(doc->>'client_id') = $1" in where
    assert "(doc->'is_deleted') IS DISTINCT FROM to_jsonb($2::boolean)" in where
    assert params == ["c-1", True]


def test_sql_where_placeholder_bug_fixed():
    """QwenWork issue 1: {"field": {"$ne": None}} must NEVER skip a parameter index."""
    # When only $ne: None is queried
    where, params = _build_sql_where({"notes": {"$ne": None}})
    assert where == " WHERE (doc ? 'notes' AND doc->'notes' != 'null'::jsonb)"
    assert params == []

    # When $ne: None is followed by another condition
    where, params = _build_sql_where({"notes": {"$ne": None}, "status": "active"})
    assert "(doc ? 'notes' AND doc->'notes' != 'null'::jsonb)" in where
    assert "(doc->>'status') = $1" in where
    assert params == ["active"]
    assert len(params) == 1

    # When preceded and followed by parameters
    where, params = _build_sql_where({
        "client_id": "c1",
        "notes": {"$ne": None},
        "status": "completed",
    })
    assert "(doc->>'client_id') = $1" in where
    assert "(doc ? 'notes' AND doc->'notes' != 'null'::jsonb)" in where
    assert "(doc->>'status') = $2" in where
    assert params == ["c1", "completed"]
    assert len(params) == 2


def test_sql_sort_builder_and_security():
    # Numeric field sorting (uses numeric cast)
    sort_sql = _build_sql_sort([("confidence", -1)])
    assert "ORDER BY COALESCE((doc->>'confidence')::numeric, 0) DESC NULLS FIRST" == sort_sql.strip()

    # Text / ISO date sorting
    sort_sql = _build_sql_sort("uploaded_at")
    assert "ORDER BY (doc->>'uploaded_at') ASC NULLS LAST" == sort_sql.strip()

    # Multiple sort fields
    sort_sql = _build_sql_sort([("created_at", -1), ("name", 1)])
    assert "(doc->>'created_at') DESC NULLS FIRST, (doc->>'name') ASC NULLS LAST" in sort_sql

    # Security check: rejects invalid SQL identifiers
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _build_sql_where({"malicious;DROP TABLE files;--": "attack"})

    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _build_sql_sort([("malicious;DROP TABLE", 1)])


# ===========================================================================
# Requirement 2: Fail Closed on Unknown Operators
# ===========================================================================

def test_fail_closed_on_unsupported_operators():
    """QwenWork issue 2: Unsupported filter operators must raise ValueError."""
    # In PostgreSQL SQL builder
    with pytest.raises(ValueError, match="Unsupported filter operator: \\$gt"):
        _build_sql_where({"confidence": {"$gt": 5}})

    with pytest.raises(ValueError, match="Unsupported filter operator: \\$regex"):
        _build_sql_where({"name": {"$regex": "^report"}})

    with pytest.raises(ValueError, match="Unsupported filter operator: \\$lt"):
        _build_sql_where({"id": {"$lt": "xyz"}})

    # In MemoryDatabase _match
    doc = {"confidence": 10, "name": "sample.pdf"}
    with pytest.raises(ValueError, match="Unsupported filter operator: \\$gt"):
        _match(doc, {"confidence": {"$gt": 5}})

    with pytest.raises(ValueError, match="Unsupported filter operator: \\$where"):
        _match(doc, {"name": {"$where": "this.name == 'sample.pdf'"}})


# ===========================================================================
# Requirement 4: Protect Empty Filter Operations
# ===========================================================================

def test_empty_filter_protection():
    """QwenWork issue 4: find_one({}) and delete_one({}) must raise ValueError."""
    async def _run():
        mem_db = MemoryDatabase()
        await mem_db.init()

        # find_one({}) and find_one(None) must raise
        with pytest.raises(ValueError, match="find_one requires a non-empty filter"):
            await mem_db.clients.find_one({})

        with pytest.raises(ValueError, match="find_one requires a non-empty filter"):
            await mem_db.clients.find_one(None)

        # delete_one({}) and delete_one(None) must raise
        with pytest.raises(ValueError, match="delete_one requires a non-empty filter"):
            await mem_db.clients.delete_one({})

        with pytest.raises(ValueError, match="delete_one requires a non-empty filter"):
            await mem_db.clients.delete_one(None)

        # PostgresDatabase collection must also enforce the same guards
        pg_db = PostgresDatabase("postgresql://user:pass@localhost:5432/test")
        with pytest.raises(ValueError, match="find_one requires a non-empty filter"):
            await pg_db.clients.find_one({})

        with pytest.raises(ValueError, match="delete_one requires a non-empty filter"):
            await pg_db.clients.delete_one({})

    asyncio.run(_run())


# ===========================================================================
# Requirement 3: Atomic insert_many, cascade delete, and scan finalization
# ===========================================================================

def test_atomic_insert_many_rollback():
    """QwenWork issue 3: If insert_many fails midway, all inserts in that batch roll back."""
    async def _run():
        mem_db = MemoryDatabase()
        await mem_db.init()

        # Initial state: 0 findings
        assert await mem_db.findings.count_documents({}) == 0

        # Batch where the second document will trigger an error (e.g. missing 'id' or bad type)
        bad_batch = [
            {"id": "valid-1", "title": "Valid"},
            {"title": "Invalid missing id"},  # KeyError on d["id"]
        ]

        with pytest.raises(KeyError):
            await mem_db.findings.insert_many(bad_batch)

        # Must have rolled back: no partial findings should remain
        assert await mem_db.findings.count_documents({}) == 0
        assert await mem_db.findings.find_one({"id": "valid-1"}) is None

    asyncio.run(_run())


def test_atomic_scan_finalization_and_cascade_delete():
    """QwenWork issue 3: finalize_scan_atomic and delete_client_cascade operations."""
    async def _run():
        mem_db = MemoryDatabase()
        await mem_db.init()

        client_id = "test-client-1"
        scan_id = "test-scan-1"

        await mem_db.clients.insert_one({"id": client_id, "name": "Atomic Corp"})
        await mem_db.files.insert_one({"id": "file-1", "client_id": client_id, "name": "f1.pdf"})
        await mem_db.scans.insert_one({"id": scan_id, "client_id": client_id, "status": "scanning"})

        # Atomic scan finalization
        findings = [
            {"id": "fnd-1", "scan_id": scan_id, "category": "duplicate", "status": "unreviewed"},
            {"id": "fnd-2", "scan_id": scan_id, "category": "missing_doc", "status": "unreviewed"},
        ]
        scan_update = {"status": "completed", "progress": 100, "total_findings": 2}
        await mem_db.finalize_scan_atomic(scan_id, scan_update, findings)

        scan = await mem_db.scans.find_one({"id": scan_id})
        assert scan["status"] == "completed"
        assert await mem_db.findings.count_documents({"scan_id": scan_id}) == 2

        # Atomic cascade delete
        await mem_db.delete_client_cascade(client_id)
        assert await mem_db.clients.find_one({"id": client_id}) is None
        assert await mem_db.files.count_documents({"client_id": client_id}) == 0
        assert await mem_db.scans.count_documents({"client_id": client_id}) == 0
        assert await mem_db.findings.count_documents({"scan_id": scan_id}) == 0

    asyncio.run(_run())


# ===========================================================================
# Requirement D: Missing DATABASE_URL in postgres mode
# ===========================================================================

def test_missing_database_url_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_BACKEND", "postgres")

    with pytest.raises(ValueError) as exc_info:
        get_database(database_url="", backend="postgres", reset=True)
    assert "DATABASE_URL is required when PostgreSQL backend is configured" in str(exc_info.value)


# ===========================================================================
# Requirement C: PostgreSQL connection failure raises error, no memory fallback
# ===========================================================================

def test_postgres_connection_failure_raises_and_never_falls_back():
    async def _run():
        bad_url = "postgresql://invalid_user:invalid_pass@127.0.0.1:59999/nonexistent_db"
        db = PostgresDatabase(bad_url)

        # Must raise RuntimeError immediately on init
        with pytest.raises(RuntimeError) as exc_info:
            await db.init()
        assert "PostgreSQL connection failed" in str(exc_info.value)

        # Must NOT have any memory fallback store
        assert not hasattr(db, "_memory_data")
        assert db._pg_pool is None

        # Accessing pool must fail clearly
        with pytest.raises(RuntimeError, match="PostgreSQL connection pool is not initialized"):
            _ = db.pool

    asyncio.run(_run())


# ===========================================================================
# Requirement E: Explicit in-memory database provider for unit tests
# ===========================================================================

def test_explicit_memory_database_provider():
    async def _run():
        mem_db = get_database(backend="memory", reset=True)
        assert isinstance(mem_db, MemoryDatabase)

        # Test basic collection operations
        await mem_db.init()
        await mem_db.clients.insert_one({"id": "mem-1", "name": "Memory Client"})
        doc = await mem_db.clients.find_one({"id": "mem-1"})
        assert doc is not None and doc["name"] == "Memory Client"

        count = await mem_db.clients.count_documents({"id": "mem-1"})
        assert count == 1

        del_res = await mem_db.clients.delete_one({"id": "mem-1"})
        assert del_res.deleted_count == 1
        assert await mem_db.clients.count_documents({"id": "mem-1"}) == 0
        await mem_db.close()

    asyncio.run(_run())


# ===========================================================================
# Requirement F: Repository operations across all collections
# ===========================================================================

def test_repository_operations_all_collections():
    async def _run():
        mem_db = MemoryDatabase()
        await mem_db.init()

        # 1. Firm
        await mem_db.firm.update_one({"id": "firm"}, {"$set": {"name": "Test Firm"}}, upsert=True)
        firm = await mem_db.firm.find_one({"id": "firm"})
        assert firm["name"] == "Test Firm"

        # 2. Clients
        client_id = str(uuid.uuid4())
        await mem_db.clients.insert_one({"id": client_id, "name": "Client Alpha", "created_at": "2024-01-01"})
        client = await mem_db.clients.find_one({"id": client_id})
        assert client["name"] == "Client Alpha"

        # 3. Files
        file_id = str(uuid.uuid4())
        await mem_db.files.insert_one({
            "id": file_id,
            "client_id": client_id,
            "name": "ledger.csv",
            "is_deleted": False,
            "uploaded_at": "2024-01-02",
        })
        active_files = await mem_db.files.find(
            {"client_id": client_id, "is_deleted": {"$ne": True}}
        ).to_list(100)
        assert len(active_files) == 1

        # Soft delete file
        await mem_db.files.update_one({"id": file_id}, {"$set": {"is_deleted": True}})
        active_files = await mem_db.files.find(
            {"client_id": client_id, "is_deleted": {"$ne": True}}
        ).to_list(100)
        assert len(active_files) == 0

        # 4. Scans
        scan_id = str(uuid.uuid4())
        await mem_db.scans.insert_one({
            "id": scan_id,
            "client_id": client_id,
            "status": "queued",
            "progress": 0,
        })
        await mem_db.scans.update_one({"id": scan_id}, {"$set": {"status": "completed", "progress": 100}})
        scan = await mem_db.scans.find_one({"id": scan_id})
        assert scan["status"] == "completed"
        assert scan["progress"] == 100

        # 5. Findings
        findings = [
            {"id": f"f-{i}", "scan_id": scan_id, "category": "duplicate", "confidence": 90 + i, "status": "unreviewed"}
            for i in range(3)
        ]
        ins_res = await mem_db.findings.insert_many(findings)
        assert len(ins_res.inserted_ids) == 3

        # Filter findings
        unreviewed = await mem_db.findings.find({"scan_id": scan_id, "status": "unreviewed"}).to_list(10)
        assert len(unreviewed) == 3

        # Update review status
        await mem_db.findings.update_one({"id": "f-0"}, {"$set": {"status": "keep"}})
        updated_f = await mem_db.findings.find_one({"id": "f-0"})
        assert updated_f["status"] == "keep"

        # Delete many
        del_findings = await mem_db.findings.delete_many({"scan_id": scan_id})
        assert del_findings.deleted_count == 3
        await mem_db.close()

    asyncio.run(_run())


# ===========================================================================
# Requirements A & B: Real PostgreSQL tests (Cleanly skipped if no Postgres)
# ===========================================================================

def test_real_postgres_client_create_and_retrieve():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not asyncio.run(_can_connect_postgres(url)):
        pytest.skip(
            "Real PostgreSQL server is unavailable; cleanly skipping real PostgreSQL test per verification rules."
        )

    async def _run():
        db = PostgresDatabase(url)
        await db.init()
        test_id = f"test-client-{uuid.uuid4()}"

        try:
            # Create
            await db.clients.insert_one({"id": test_id, "name": "Real Postgres Client", "notes": "Automated verification"})
            # Retrieve
            doc = await db.clients.find_one({"id": test_id})
            assert doc is not None, "Client was not persisted to PostgreSQL table"
            assert doc["name"] == "Real Postgres Client"
        finally:
            await db.clients.delete_one({"id": test_id})
            await db.close()

    asyncio.run(_run())


def test_real_postgres_persistence_across_reinitialization():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not asyncio.run(_can_connect_postgres(url)):
        pytest.skip(
            "Real PostgreSQL server is unavailable; cleanly skipping persistence test per verification rules."
        )

    async def _run():
        test_id = f"test-persist-{uuid.uuid4()}"

        # First session: write data and close connection pool
        db1 = PostgresDatabase(url)
        await db1.init()
        await db1.clients.insert_one({"id": test_id, "name": "Persistent Client Across Reinit"})
        await db1.close()

        # Second session: new database instance reconnects to PostgreSQL
        db2 = PostgresDatabase(url)
        await db2.init()
        try:
            retrieved = await db2.clients.find_one({"id": test_id})
            assert retrieved is not None, "Data was not persistent in PostgreSQL across connection reinitialization"
            assert retrieved["name"] == "Persistent Client Across Reinit"
        finally:
            await db2.clients.delete_one({"id": test_id})
            await db2.close()

    asyncio.run(_run())
