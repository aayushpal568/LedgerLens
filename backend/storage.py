"""Emergent object storage client.

Persists uploaded document bytes so they survive across deployments. The
processing engine still reads from a local path, so during a scan we download
objects to a temp file — this keeps the engine portable for the future local
(Tauri) desktop build.
"""
import os
from pathlib import Path

import requests

STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = "acct-doc-checker"

LOCAL_DIR = Path(__file__).resolve().parent / "uploads"
_use_local_fallback = False
_storage_key = None



def init_storage(force: bool = False):
    global _storage_key, _use_local_fallback
    if _use_local_fallback:
        return "local"
    if _storage_key and not force:
        return _storage_key
    emergent_key = os.environ.get("EMERGENT_LLM_KEY")
    if not emergent_key:
        _use_local_fallback = True
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        return "local"
    try:
        resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": emergent_key}, timeout=5)
        resp.raise_for_status()
        _storage_key = resp.json()["storage_key"]
        return _storage_key
    except Exception:
        _use_local_fallback = True
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        return "local"


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    if _use_local_fallback or key == "local":
        target = LOCAL_DIR / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "size": len(data)}

    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data, timeout=120,
    )
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.put(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data, timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def get_object(path: str) -> bytes:
    key = init_storage()
    if _use_local_fallback or key == "local":
        target = LOCAL_DIR / path
        if not target.exists():
            raise FileNotFoundError(f"Object not found: {path}")
        return target.read_bytes()

    resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=120)
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=120)
    resp.raise_for_status()
    return resp.content



MIME_TYPES = {
    "pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "tiff": "image/tiff", "tif": "image/tiff",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def mime_for(ext: str) -> str:
    return MIME_TYPES.get((ext or "").lower(), "application/octet-stream")
