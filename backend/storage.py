"""Production object storage client for LedgerLens.

Supports:
1. S3 / Cloudflare R2 object storage via boto3 (when S3_BUCKET is configured).
2. Emergent object storage proxy (when EMERGENT_LLM_KEY is configured).
3. Local disk fallback ONLY when STORAGE_BACKEND=local or in hermetic memory testing.

Fails clearly in production when no production storage backend is configured.
"""
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = "acct-doc-checker"

LOCAL_DIR = Path(__file__).resolve().parent / "uploads"
_use_local_fallback = False
_storage_key = None
_s3_client = None

MIME_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def mime_for(ext: str) -> str:
    return MIME_TYPES.get((ext or "").lower(), "application/octet-stream")


def reset_storage() -> None:
    """Reset storage state (for testing)."""
    global _storage_key, _use_local_fallback, _s3_client
    _storage_key = None
    _use_local_fallback = False
    _s3_client = None


def init_storage(force: bool = False) -> str:
    """Initialize object storage client.

    Returns storage mode identifier: 's3', 'local', or emergent storage key.
    Fails clearly if production storage is unconfigured.
    """
    global _storage_key, _use_local_fallback, _s3_client

    if not force:
        if _s3_client is not None:
            return "s3"
        if _use_local_fallback:
            return "local"
        if _storage_key:
            return _storage_key

    s3_bucket = (os.environ.get("S3_BUCKET") or "").strip()
    storage_backend = (os.environ.get("STORAGE_BACKEND") or "").lower().strip()
    data_backend = (os.environ.get("DATA_BACKEND") or "postgres").lower().strip()
    is_testing = os.environ.get("TESTING", "").lower() in ("true", "1") or data_backend == "memory"

    # 1. S3 / Cloudflare R2 via boto3
    if s3_bucket:
        try:
            import boto3
            from botocore.config import Config

            s3_region = (os.environ.get("S3_REGION") or os.environ.get("AWS_REGION") or "us-east-1").strip()
            endpoint_url = (os.environ.get("S3_ENDPOINT_URL") or "").strip() or None
            ak = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip() or None
            sk = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip() or None

            kwargs = {
                "region_name": s3_region,
                "config": Config(signature_version="s3v4", retries={"max_attempts": 3}),
            }
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            if ak and sk:
                kwargs["aws_access_key_id"] = ak
                kwargs["aws_secret_access_key"] = sk

            _s3_client = boto3.client("s3", **kwargs)
            logger.info("Initialized S3/R2 object storage with bucket: %s", s3_bucket)
            return "s3"
        except Exception as e:
            logger.error("Failed to initialize S3 object storage: %s", e)
            raise RuntimeError(f"Failed to initialize S3 object storage: {e}") from e

    # 2. Emergent Object Storage
    emergent_key = os.environ.get("EMERGENT_LLM_KEY")
    if emergent_key:
        try:
            resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": emergent_key}, timeout=5)
            resp.raise_for_status()
            _storage_key = resp.json()["storage_key"]
            return _storage_key
        except Exception as e:
            logger.warning("Emergent storage init failed: %s", e)

    # 3. Explicit Local Disk Fallback (allowed in testing or when STORAGE_BACKEND=local)
    if storage_backend == "local" or is_testing or not os.environ.get("DATABASE_URL"):
        _use_local_fallback = True
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        return "local"

    # 4. Fail-closed for production cloud without storage
    raise RuntimeError(
        "Production object storage is not configured. "
        "Please configure S3_BUCKET (and AWS credentials) or set STORAGE_BACKEND=local for local development."
    )


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()

    if key == "s3" and _s3_client is not None:
        s3_bucket = (os.environ.get("S3_BUCKET") or "").strip()
        _s3_client.put_object(
            Bucket=s3_bucket,
            Key=path,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return {"path": path, "size": len(data)}

    if _use_local_fallback or key == "local":
        target = LOCAL_DIR / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "size": len(data)}

    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=120,
    )
    if resp.status_code == 404:
        key = init_storage(force=True)
        resp = requests.put(
            f"{STORAGE_URL}/objects/{path}",
            headers={"X-Storage-Key": key, "Content-Type": content_type},
            data=data,
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def get_object(path: str) -> bytes:
    key = init_storage()

    if key == "s3" and _s3_client is not None:
        s3_bucket = (os.environ.get("S3_BUCKET") or "").strip()
        try:
            resp = _s3_client.get_object(Bucket=s3_bucket, Key=path)
            return resp["Body"].read()
        except Exception as e:
            if "NoSuchKey" in str(e) or "404" in str(e):
                raise FileNotFoundError(f"Object not found: {path}") from e
            raise

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


def delete_object(path: str) -> bool:
    """Delete an object from S3, local, or emergent object storage."""
    key = init_storage()

    if key == "s3" and _s3_client is not None:
        s3_bucket = (os.environ.get("S3_BUCKET") or "").strip()
        try:
            _s3_client.delete_object(Bucket=s3_bucket, Key=path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to delete object '%s' from S3/R2 bucket '%s': %s", path, s3_bucket, e)
            return False

    if _use_local_fallback or key == "local":
        target = LOCAL_DIR / path
        if target.exists():
            try:
                target.unlink()
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to delete local object '%s': %s", path, e)
                return False
        return True

    try:
        resp = requests.delete(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=30)
        if resp.status_code not in (200, 204, 404):
            logger.warning("Failed to delete object '%s' via storage proxy: HTTP %s", path, resp.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to delete object '%s' via storage proxy: %s", path, e)
        return False
