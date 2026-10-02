/**
 * Resilient scan-status polling.
 *
 * Extracted from ScanWorkspace so the retry/give-up policy is unit-testable.
 * The bug this guards: the previous inline poller cleared its interval on the
 * FIRST thrown error, so a single transient network blip (connection reset,
 * brief 5xx, dropped keep-alive) permanently froze the "Scanning…" UI and never
 * loaded results, even though the scan completed successfully server-side.
 *
 * Policy:
 *  - Poll getScan(scanId) every intervalMs.
 *  - A transient error does NOT abort polling; we tolerate up to
 *    maxConsecutiveErrors consecutive failures before giving up. Any success
 *    resets the failure budget.
 *  - Stop and report onError if failures exceed the budget OR total elapsed
 *    exceeds maxDurationMs (guards against an orphaned scan polling forever).
 *  - On a terminal status, stop and call onDone(scan).
 *
 * `setInterval`/`clearInterval` are injectable for deterministic tests.
 */
export const TERMINAL_SCAN_STATUSES = ["completed", "cancelled", "error"];

export function startScanPolling({
  scanId,
  getScan,
  onState,
  onDone,
  onError,
  intervalMs = 1000,
  maxConsecutiveErrors = 5,
  maxDurationMs = 10 * 60 * 1000,
  setInterval: setIntervalImpl = setInterval,
  clearInterval: clearIntervalImpl = clearInterval,
  now = () => Date.now(),
}) {
  let timer = null;
  let consecutiveErrors = 0;
  let done = false;
  const startedAt = now();

  const stop = () => {
    done = true;
    if (timer !== null) {
      clearIntervalImpl(timer);
      timer = null;
    }
  };

  const tick = async () => {
    if (done) return;
    if (now() - startedAt > maxDurationMs) {
      stop();
      if (onError) onError("Scan timed out while waiting for a response.");
      return;
    }
    try {
      const s = await getScan(scanId);
      consecutiveErrors = 0; // a successful poll resets the transient-error budget
      if (onState) onState(s);
      if (s && TERMINAL_SCAN_STATUSES.includes(s.status)) {
        stop();
        if (onDone) onDone(s);
      }
    } catch (err) {
      consecutiveErrors += 1;
      // Transient error: keep polling unless we've exhausted the budget.
      if (consecutiveErrors >= maxConsecutiveErrors) {
        stop();
        if (onError) onError("Lost connection to the scan service");
      }
    }
  };

  timer = setIntervalImpl(tick, intervalMs);
  return { stop, tick };
}
