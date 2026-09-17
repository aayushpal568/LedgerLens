"""Tests for the local SQLite data store (Mongo-compatible adapter).

Exercises exactly the operations server.py relies on, so the offline desktop
build behaves like the Mongo web build.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlite_store import SqliteDatabase  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = SqliteDatabase(str(tmp_path / "test.db"))
    yield d
    d.close()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_insert_and_find_one(db):
    run(db.clients.insert_one({"id": "c1", "name": "Acme", "created_at": "2024-01-01"}))
    doc = run(db.clients.find_one({"id": "c1"}))
    assert doc["name"] == "Acme"
    assert "_id" not in doc


def test_projection_exclusion_and_inclusion(db):
    run(db.files.insert_one({"id": "f1", "client_id": "c1", "name": "a.pdf", "storage_path": "secret"}))
    excl = run(db.files.find_one({"id": "f1"}, {"_id": 0, "storage_path": 0}))
    assert "storage_path" not in excl and excl["name"] == "a.pdf"
    incl = run(db.files.find({"client_id": "c1"}, {"id": 1}).to_list(10))
    assert incl == [{"id": "f1"}]


def test_find_sort_and_to_list(db):
    for i, ts in enumerate(["2024-01-03", "2024-01-01", "2024-01-02"]):
        run(db.scans.insert_one({"id": f"s{i}", "client_id": "c1", "started_at": ts}))
    rows = run(db.scans.find({"client_id": "c1"}, {"_id": 0}).sort("started_at", -1).to_list(1000))
    assert [r["started_at"] for r in rows] == ["2024-01-03", "2024-01-02", "2024-01-01"]


def test_find_one_with_sort_kwarg(db):
    run(db.scans.insert_one({"id": "s1", "client_id": "c2", "started_at": "2024-01-01"}))
    run(db.scans.insert_one({"id": "s2", "client_id": "c2", "started_at": "2024-05-01"}))
    latest = run(db.scans.find_one({"client_id": "c2"}, {"_id": 0}, sort=[("started_at", -1)]))
    assert latest["id"] == "s2"


def test_update_one_set_and_upsert(db):
    run(db.firm.update_one({"id": "firm"}, {"$set": {"name": "R Co"}}, upsert=True))
    doc = run(db.firm.find_one({"id": "firm"}))
    assert doc["name"] == "R Co"
    run(db.firm.update_one({"id": "firm"}, {"$set": {"name": "R Co 2"}}, upsert=True))
    doc = run(db.firm.find_one({"id": "firm"}))
    assert doc["name"] == "R Co 2"
    assert run(db.firm.count_documents({})) == 1


def test_ne_operator_soft_delete(db):
    run(db.files.insert_one({"id": "f1", "client_id": "c1"}))
    run(db.files.insert_one({"id": "f2", "client_id": "c1", "is_deleted": True}))
    active = run(db.files.find({"client_id": "c1", "is_deleted": {"$ne": True}}, {"_id": 0}).to_list(100))
    assert {f["id"] for f in active} == {"f1"}


def test_insert_many_and_delete_many(db):
    run(db.findings.insert_many([{"id": f"x{i}", "scan_id": "s1"} for i in range(5)]))
    assert run(db.findings.count_documents({"scan_id": "s1"})) == 5
    res = run(db.findings.delete_many({"scan_id": "s1"}))
    assert res.deleted_count == 5
    assert run(db.findings.count_documents({})) == 0


def test_delete_one(db):
    run(db.clients.insert_one({"id": "c9", "name": "Z"}))
    run(db.clients.delete_one({"id": "c9"}))
    assert run(db.clients.find_one({"id": "c9"})) is None


def test_persists_across_reopen(tmp_path):
    path = str(tmp_path / "persist.db")
    d1 = SqliteDatabase(path)
    run(d1.templates.insert_one({"id": "t1", "name": "Tpl"}))
    d1.close()
    d2 = SqliteDatabase(path)
    assert run(d2.templates.find_one({"id": "t1"}))["name"] == "Tpl"
    d2.close()
