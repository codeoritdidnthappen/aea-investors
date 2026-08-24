// TICK-026: reproducible load test for NFR-13 (chat/API, <3.0s p95 at 20 VU/60s).
//
// ai_server/app/auth.py's session cookie is HttpOnly by design (never exposed to
// page JS or any external process -- the same posture that keeps the raw OAuth
// token off the browser entirely). k6 (an external process) cannot attach it, so
// this scenario runs *inside* an already-authenticated browser tab instead: paste
// this whole script into DevTools Console on https://chat.localhost/ after logging
// in for real, and it drives the load itself via same-origin fetch() calls, which
// the browser attaches the cookie to automatically.
//
// Adjust CONCURRENCY/DURATION_MS below to match the scenario being run (5 VU/30s
// for a cost-contained trial, 20 VU/60s for the ticket's own documented target).
// See scripts/k6_performance_test.js for the companion OpenEMR scheduling
// read/write scenario (NFR-14), which needs only a bearer token, no cookie, and
// runs as ordinary k6.

(async () => {
  const CONCURRENCY = 5;
  const DURATION_MS = 30000;
  const MESSAGE = "What are my upcoming appointments?"; // stays on the turn service's
  // path, never onboarding's, which has no comparable NFR-13 target here.

  const start = performance.now();
  const latencies = [];
  const errors = [];

  async function oneRequest() {
    const t0 = performance.now();
    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: MESSAGE }),
      });
      await resp.text();
      const t1 = performance.now();
      if (!resp.ok) {
        errors.push(resp.status);
      } else {
        latencies.push(t1 - t0);
      }
    } catch (e) {
      errors.push(String(e));
    }
  }

  async function worker() {
    while (performance.now() - start < DURATION_MS) {
      await oneRequest();
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));

  latencies.sort((a, b) => a - b);
  function pct(p) {
    if (latencies.length === 0) return null;
    const idx = Math.min(latencies.length - 1, Math.floor((p / 100) * latencies.length));
    return latencies[idx];
  }
  const report = {
    total_requests: latencies.length + errors.length,
    successful: latencies.length,
    errors: errors.length,
    error_samples: errors.slice(0, 5),
    p50_ms: pct(50),
    p95_ms: pct(95),
    p99_ms: pct(99),
    min_ms: latencies[0],
    max_ms: latencies[latencies.length - 1],
    duration_ms: performance.now() - start,
  };
  console.log(JSON.stringify(report, null, 2));
  return report;
})();
