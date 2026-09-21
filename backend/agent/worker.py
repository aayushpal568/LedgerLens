"""Durable background worker for LedgerLens Claude agent runs (Step 5).

Runs inside the existing FastAPI/asyncio + PostgreSQL architecture. It does NOT
introduce a queue broker (Redis/Celery/Kafka) or a new agent framework: the
PostgreSQL agent_runs table is the queue and the single source of truth.

Responsibilities
----------------
1. Atomically claim queued runs (via the durable DB claim) so that two workers can
   never execute the same run. Each claimed run is executed by the existing bounded
   agent loop (Claude orchestrator + registered read-only tools).
2. Periodically recover stale 'running' runs whose lease expired (dead/crashed worker),
   requeuing them for retry (or failing them once max attempts are exhausted).
3. Respect cancellation/terminal states: a cancelled or completed run is never claimed,
   so retries can never resurrect a cancelled run, and duplicates cannot execute twice.

Everything remains tenant-scoped: the worker rebuilds the AuthedUser context strictly
from the persisted run's firm_id / created_by (server-side values), never trusting the
request/Claude for firm_id.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import services
from auth_dep import AuthedUser

logger = logging.getLogger(__name__)


def worker_enabled() -> bool:
    """The durable poll worker is opt-in via env, and off by default on the memory/test backend.

    Production (PostgreSQL) enables it with AGENT_WORKER_ENABLED=true so that runs enqueued
    while no request handler is co-located (or orphaned by a crash) are still drained.
    """
    raw = str(os.environ.get("AGENT_WORKER_ENABLED", "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Default: enabled only when running against the authoritative PostgreSQL backend.
    return str(os.environ.get("DATA_BACKEND", "postgres")).strip().lower() == "postgres"


def _user_from_run(run: Dict[str, Any]) -> Optional[AuthedUser]:
    """Reconstruct a tenant-scoped AuthedUser from a persisted run document (server-side only)."""
    firm_id = str(run.get("firm_id") or "").strip()
    if not firm_id:
        return None
    user_id = str(run.get("created_by") or "agent-worker")
    return AuthedUser(user_id=user_id, firm_id=firm_id, token_version=0)


async def process_next_queued_run(db=None, llm_provider: Optional[Any] = None, registry: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Claim and execute at most one queued run. Returns the loop result, or None if idle."""
    database = services._get_db(db)
    claimed = await database.claim_next_queued_agent_run(
        worker_id=services.WORKER_ID,
        firm_id=None,  # worker is cross-tenant; each run stays scoped via _user_from_run
        lease_seconds=services.agent_lease_seconds(),
    )
    if not claimed:
        return None
    user = _user_from_run(claimed)
    if not user:
        logger.error("Claimed run %s without a resolvable firm_id; failing it.", claimed.get("id"))
        try:
            await database.agent_runs.update_one(
                {"id": claimed["id"]},
                {"$set": {
                    "status": services.RUN_STATUS_FAILED,
                    "error": "Run is missing tenant context and cannot be executed safely.",
                    "completed_at": services.now_iso(),
                    "lease_expires_at": None,
                    "worker_id": None,
                }},
            )
        except Exception:
            pass
        return None

    from agent.loop import run_agent_loop
    return await run_agent_loop(
        user,
        str(claimed["id"]),
        str(claimed["thread_id"]),
        llm_provider=llm_provider,
        registry=registry,
        db=database,
    )


async def recover_once(db=None) -> Dict[str, int]:
    """Run lease-aware stale recovery once (crash / restart recovery)."""
    return await services.recover_stale_agent_runs(db=db)


async def worker_loop(
    stop_event: asyncio.Event,
    poll_interval: float = 2.0,
    recovery_interval: float = 30.0,
    db=None,
    max_consecutive_drain: int = 16,
) -> None:
    """Long-running poll loop: recover stale runs, then drain the queued run backlog."""
    logger.info("LedgerLens agent worker started (worker_id=%s)", services.WORKER_ID)
    last_recovery = 0.0
    try:
        while not stop_event.is_set():
            now = asyncio.get_event_loop().time()
            if now - last_recovery >= recovery_interval:
                try:
                    res = await recover_once(db=db)
                    if res.get("requeued") or res.get("failed"):
                        logger.info("Stale agent run recovery: %s", res)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Stale run recovery skipped: %s", e)
                last_recovery = now

            drained = 0
            while drained < max_consecutive_drain and not stop_event.is_set():
                result = await process_next_queued_run(db=db)
                if result is None:
                    break
                drained += 1

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("LedgerLens agent worker stopped.")


class AgentWorkerHandle:
    """Manages the lifecycle of the in-process worker asyncio task."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, db=None, poll_interval: float = 2.0) -> bool:
        if self.running:
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            worker_loop(self._stop, poll_interval=poll_interval, db=db)
        )
        return True

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()
        self._task = None
        self._stop = None


# Process-wide singleton used by the FastAPI startup/shutdown hooks.
_worker_handle = AgentWorkerHandle()


def get_worker_handle() -> AgentWorkerHandle:
    return _worker_handle


async def start_worker_if_enabled(db=None) -> bool:
    if not worker_enabled():
        logger.info("LedgerLens agent worker disabled (AGENT_WORKER_ENABLED / DATA_BACKEND).")
        return False
    return await _worker_handle.start(db=db)


async def stop_worker() -> None:
    await _worker_handle.stop()
