"""Local SQLite data store with a Mongo-compatible async surface.

This lets the SAME `server.py` code run against MongoDB (web/cloud build) or a
single local SQLite file (offline Windows/Tauri build) by only swapping the
`db` object — no query rewrites. It implements the small subset of the motor
API that server.py uses.

Documents are stored as JSON blobs keyed by their `id`. Filtering, projection
and sorting are done in Python; volumes are small (one firm, single user), so
this is simple and robust. Suitable for local desktop use, not high concurrency.
"""
import json
import sqlite3
import threading
from typing import Dict, List, Optional

_SAFE_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


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
    if keep:  # inclusion projection
        return {k: d[k] for k in keep if k in d}
    drop = [k for k, v in proj.items() if v == 0]
    for k in drop:
        d.pop(k, None)
    return d


def _sort(rows: List[dict], sort) -> List[dict]:
    if not sort:
        return rows
    # accept ("field", dir) or [("field", dir), ...]
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
        rows = self._c._read_all()
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
    def __init__(self, db: "SqliteDatabase", name: str):
        if not name or any(ch not in _SAFE_NAME for ch in name):
            raise ValueError(f"Unsafe collection name: {name}")
        self._db = db
        self.name = name
        self._ensure()

    def _ensure(self):
        with self._db._lock:
            self._db._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.name} (id TEXT PRIMARY KEY, doc TEXT NOT NULL)"
            )
            self._db._conn.commit()

    def _read_all(self) -> List[dict]:
        with self._db._lock:
            cur = self._db._conn.execute(f"SELECT doc FROM {self.name}")
            return [json.loads(r[0]) for r in cur.fetchall()]

    def _write(self, doc: dict):
        with self._db._lock:
            self._db._conn.execute(
                f"INSERT INTO {self.name} (id, doc) VALUES (?, ?) "
                f"ON CONFLICT(id) DO UPDATE SET doc=excluded.doc",
                (str(doc["id"]), json.dumps(doc)),
            )
            self._db._conn.commit()

    # ---- Mongo-compatible async methods ----
    async def find_one(self, filt: dict, projection: Optional[dict] = None, sort=None):
        rows = [r for r in self._read_all() if _match(r, filt)]
        rows = _sort(rows, sort)
        return _project(rows[0], projection) if rows else None

    def find(self, filt: Optional[dict] = None, projection: Optional[dict] = None):
        return _Cursor(self, filt or {}, projection)

    async def count_documents(self, filt: Optional[dict] = None):
        return sum(1 for r in self._read_all() if _match(r, filt or {}))

    async def insert_one(self, doc: dict):
        d = dict(doc)
        d.pop("_id", None)
        self._write(d)
        return _InsertResult([d["id"]])

    async def insert_many(self, docs: List[dict]):
        ids = []
        for doc in docs:
            d = dict(doc)
            d.pop("_id", None)
            self._write(d)
            ids.append(d["id"])
        return _InsertResult(ids)

    async def update_one(self, filt: dict, update: dict, upsert: bool = False):
        rows = self._read_all()
        target = next((r for r in rows if _match(r, filt)), None)
        set_fields = update.get("$set", {})
        if target is not None:
            target.update(set_fields)
            self._write(target)
            return _UpdateResult(1, 1)
        if upsert:
            new_doc = {k: v for k, v in filt.items() if not isinstance(v, dict)}
            new_doc.update(set_fields)
            self._write(new_doc)
            return _UpdateResult(0, 0, upserted_id=new_doc.get("id"))
        return _UpdateResult(0, 0)

    async def delete_one(self, filt: dict):
        rows = self._read_all()
        target = next((r for r in rows if _match(r, filt)), None)
        if target is None:
            return _DeleteResult(0)
        with self._db._lock:
            self._db._conn.execute(f"DELETE FROM {self.name} WHERE id = ?", (str(target["id"]),))
            self._db._conn.commit()
        return _DeleteResult(1)

    async def delete_many(self, filt: dict):
        rows = [r for r in self._read_all() if _match(r, filt)]
        if not rows:
            return _DeleteResult(0)
        with self._db._lock:
            self._db._conn.executemany(
                f"DELETE FROM {self.name} WHERE id = ?", [(str(r["id"]),) for r in rows]
            )
            self._db._conn.commit()
        return _DeleteResult(len(rows))


class SqliteDatabase:
    """Mimics `motor` database: attribute access returns a collection."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._collections: Dict[str, _Collection] = {}

    def __getattr__(self, name: str) -> _Collection:
        if name.startswith("_"):
            raise AttributeError(name)
        cols = self.__dict__.setdefault("_collections", {})
        if name not in cols:
            cols[name] = _Collection(self, name)
        return cols[name]

    def close(self):
        with self._lock:
            self._conn.close()
