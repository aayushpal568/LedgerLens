import { startScanPolling, TERMINAL_SCAN_STATUSES } from "./scanPolling";

// Drive the poller deterministically: capture the interval callback and a
// controllable clock, then invoke the tick by hand (no real timers/waits).
function makeHarness({ getScan, maxConsecutiveErrors = 5, maxDurationMs = 10 * 60 * 1000 } = {}) {
  let cb = null;
  let cleared = false;
  let clock = 0;
  const calls = { done: [], error: [], state: [] };
  startScanPolling({
    scanId: "s-1",
    getScan,
    onState: (s) => calls.state.push(s),
    onDone: (s) => calls.done.push(s),
    onError: (m) => calls.error.push(m),
    maxConsecutiveErrors,
    maxDurationMs,
    setInterval: (fn) => { cb = fn; return 42; },
    clearInterval: () => { cleared = true; },
    now: () => clock,
  });
  return {
    tick: () => cb && cb(),
    advance: (ms) => { clock += ms; },
    isStopped: () => cleared,
    calls,
  };
}

const ok = (status, extra = {}) => async () => ({ id: "s-1", status, ...extra });
const boom = async () => { throw new Error("connection reset"); };

describe("startScanPolling — transient-error resilience", () => {
  test("a single transient error does NOT abort the poll", async () => {
    let n = 0;
    const getScan = async () => {
      n += 1;
      if (n === 1) throw new Error("blip");
      return { id: "s-1", status: "completed", total_findings: 3 };
    };
    const h = makeHarness({ getScan });
    await h.tick();           // fails once
    expect(h.isStopped()).toBe(false); // BUG GUARD: must keep polling
    expect(h.calls.error).toHaveLength(0);
    await h.tick();           // succeeds
    expect(h.calls.done).toHaveLength(1);
    expect(h.calls.done[0].status).toBe("completed");
    expect(h.isStopped()).toBe(true);
  });

  test("gives up only after maxConsecutiveErrors consecutive failures", async () => {
    const h = makeHarness({ getScan: boom, maxConsecutiveErrors: 3 });
    await h.tick(); await h.tick();
    expect(h.isStopped()).toBe(false);
    expect(h.calls.error).toHaveLength(0);
    await h.tick(); // 3rd consecutive -> budget hit
    expect(h.isStopped()).toBe(true);
    expect(h.calls.error).toEqual(["Lost connection to the scan service"]);
  });

  test("a success resets the consecutive-failure budget", async () => {
    const beh = ["err", "err", "running", "err", "err", "err", "err", "done"];
    let i = 0;
    const getScan = async () => {
      const b = beh[i++];
      if (b === "err") throw new Error("blip");
      if (b === "done") return { id: "s-1", status: "completed", total_findings: 1 };
      return { id: "s-1", status: "running" };
    };
    const h = makeHarness({ getScan, maxConsecutiveErrors: 5 });
    await h.tick(); await h.tick();            // 2 errors
    await h.tick();                            // running resets to 0
    for (let k = 0; k < 4; k++) await h.tick();// 4 errors, under budget of 5
    expect(h.isStopped()).toBe(false);         // never gave up thanks to the reset
    await h.tick();                            // completed
    expect(h.calls.done).toHaveLength(1);
    expect(h.isStopped()).toBe(true);
  });

  test("stops with an error once maxDuration is exceeded (orphaned scan guard)", async () => {
    const h = makeHarness({ getScan: ok("running"), maxDurationMs: 5000 });
    await h.tick();
    expect(h.isStopped()).toBe(false);
    h.advance(6000);
    await h.tick();
    expect(h.isStopped()).toBe(true);
    expect(h.calls.error).toHaveLength(1);
    expect(h.calls.error[0]).toMatch(/timed out/i);
  });

  test("terminal statuses list is the UI contract", () => {
    expect(TERMINAL_SCAN_STATUSES).toEqual(expect.arrayContaining(["completed", "cancelled", "error"]));
  });
});
