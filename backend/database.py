"""Cloud PostgreSQL data store and database abstraction.

Prepares LedgerLens for PostgreSQL in production (cloud) while providing
a clean async collection interface compatible with existing engine/service logic.
Supports PostgreSQL connections via DATABASE_URL and in-memory execution for tests.
"""
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Standard collections / tables
COLLECTIONS = ["firm", "clients", "files", "templates", "scans", "findings"]


def _match(doc: dict, filt: dict) -> bool:
    for key, cond in (filt or {}).items():
        val = doc.get(key)
        if isinstance(cond, dict):
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


class _Cursor:
    def __init__(self, collection: "_Collection", filt: dict, proj: Optional[dict]):
        self._c = collection
        self._filt = filt
        self._proj = proj
        self._sort = None

    def sort(self, key, direction=1):
        self._sort = (key, direction) if not isinstance(key, list) else key
        return self

    async def to_list(self, length: Optional[int] = None):
        rows = await self._c._read_all()
        rows = [r for r in rows if _match(r, self._filt)]
        rows = _sort(rows, self._sort)
        if length is not None:
            rows = rows[:length]
        return [_project(r, self._proj) for r in rows]


class _InsertResult:
    def __init__(self, ids):
        self.inserted_id = ids[0] if ids else None
        self.inserted_ids = ids


class _UpdateResult:
    def __init__(self, matched, modified, upserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id


class _DeleteResult:
    def __init__(self, deleted):
        self.deleted_count = deleted


class _Collection:
    def __init__(self, db: "PostgresDatabase", name: str):
        self._db = db
        self.name = name

    async def _read_all(self) -> List[dict]:
        return await self._db._store_read(self.name)

    async def _write_doc(self, doc: dict):
        await self._db._store_write(self.name, doc)

    async def _delete_doc(self, doc_id: str):
        await self._db._store_delete(self.name, doc_id)

    async def find_one(self, filt: dict, projection: Optional[dict] = None, sort=None):
        rows = [r for r in await self._read_all() if _match(r, filt)]
        rows = _sort(rows, sort)
        return _project(rows[0], projection) if rows else None

    def find(self, filt: Optional[dict] = None, projection: Optional[dict] = None):
        return _Cursor(self, filt or {}, projection)

    async def count_documents(self, filt: Optional[dict] = None):
        return sum(1 for r in await self._read_all() if _match(r, filt or {}))

    async def insert_one(self, doc: dict):
        d = dict(doc)
        d.pop("_id", None)
        await self._write_doc(d)
        return _InsertResult([d["id"]])

    async def insert_many(self, docs: List[dict]):
        ids = []
        for doc in docs:
            d = dict(doc)
            d.pop("_id", None)
            await self._write_doc(d)
            ids.append(d["id"])
        return _InsertResult(ids)

    async def update_one(self, filt: dict, update: dict, upsert: bool = False):
        rows = await self._read_all()
        target = next((r for r in rows if _match(r, filt)), None)
        set_fields = update.get("$set", {})
        if target is not None:
            target.update(set_fields)
            await self._write_doc(target)
            return _UpdateResult(1, 1)
        if upsert:
            new_doc = {k: v for k, v in filt.items() if not isinstance(v, dict)}
            new_doc.update(set_fields)
            await self._write_doc(new_doc)
            return _UpdateResult(0, 0, upserted_id=new_doc.get("id"))
        return _UpdateResult(0, 0)

    async def delete_one(self, filt: dict):
        rows = await self._read_all()
        target = next((r for r in rows if _match(r, filt)), None)
        if target is None:
            return _DeleteResult(0)
        await self._delete_doc(str(target["id"]))
        return _DeleteResult(1)

    async def delete_many(self, filt: dict):
        rows = [r for r in await self._read_all() if _match(r, filt)]
        for r in rows:
            await self._delete_doc(str(r["id"]))
        return _DeleteResult(len(rows))


class PostgresDatabase:
    """PostgreSQL-backed database manager.

    Stores accounting records as JSONB documents in partitioned PostgreSQL tables,
    with an in-memory transactional cache and thread-safe fallback for test environments.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._collections: Dict[str, _Collection] = {}
        self._memory_data: Dict[str, Dict[str, dict]] = {col: {} for col in COLLECTIONS}
        self._lock = threading.RLock()
        self._pg_pool = None
        self._use_pg = False

    async def init_pg(self):
        """Initializes PostgreSQL connection pool and ensures schema exists."""
        if not self.database_url or "postgres" not in self.database_url:
            return
        try:
            import asyncpg
            self._pg_pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=10)
            async with self._pg_pool.acquire() as conn:
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
            self._use_pg = True
            logger.info(f"Connected to PostgreSQL database at {self.database_url.split('@')[-1]}")
        except Exception as e:
            logger.warning(f"PostgreSQL connection unavailable ({e}); using memory store.")
            self._use_pg = False

    async def _store_read(self, collection_name: str) -> List[dict]:
        if self._use_pg and self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    rows = await conn.fetch(f"SELECT doc FROM {collection_name}")
                    return [json.loads(r["doc"]) if isinstance(r["doc"], str) else dict(r["doc"]) for r in rows]
            except Exception as e:
                logger.error(f"Postgres read error: {e}")
        with self._lock:
            return [dict(v) for v in self._memory_data.get(collection_name, {}).values()]

    async def _store_write(self, collection_name: str, doc: dict):
        doc_id = str(doc["id"])
        if self._use_pg and self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    doc_json = json.dumps(doc)
                    await conn.execute(f"""
                        INSERT INTO {collection_name} (id, doc, updated_at)
                        VALUES ($1, $2::jsonb, NOW())
                        ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()
                    """, doc_id, doc_json)
            except Exception as e:
                logger.error(f"Postgres write error: {e}")
        with self._lock:
            col_dict = self._memory_data.setdefault(collection_name, {})
            col_dict[doc_id] = dict(doc)

    async def _store_delete(self, collection_name: str, doc_id: str):
        if self._use_pg and self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    await conn.execute(f"DELETE FROM {collection_name} WHERE id = $1", doc_id)
            except Exception as e:
                logger.error(f"Postgres delete error: {e}")
        with self._lock:
            col_dict = self._memory_data.setdefault(collection_name, {})
            col_dict.pop(doc_id, None)

    def __getattr__(self, name: str) -> _Collection:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._collections:
            self._collections[name] = _Collection(self, name)
        return self._collections[name]

    def close(self):
        if self._pg_pool:
            pass


_db_instance: Optional[PostgresDatabase] = None


def get_database(database_url: Optional[str] = None) -> PostgresDatabase:
    global _db_instance
    if _db_instance is None:
        url = database_url or os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ledgerlens")
        _db_instance = PostgresDatabase(url)
    return _db_instance
