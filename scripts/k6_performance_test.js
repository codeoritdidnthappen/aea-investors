// TICK-026: reproducible load test for NFR-13 (chat/API, <3.0s p95) and NFR-14
// (OpenEMR scheduling reads/writes, <1.0s p95), both at the documented 20 VU / 60s
// target load.
//
// The chat/API scenario requires a same-origin browser session (the ai-server's
// session cookie is HttpOnly by design -- never exposed to page JS or any external
// process, matching this project's own "raw token never reaches the browser"
// posture, see ai_server/app/auth.py). k6 cannot supply it on its own; either paste
// a real ai_session cookie value captured from an authenticated browser DevTools
// session (COOKIE env var below), or run the chat/API half via
// scripts/load_test_chat_browser.js instead (the same scenario, executed inside an
// authenticated browser tab where the cookie is already attached automatically).
//
// The scheduling scenario needs only a bearer access_token (no cookie), obtained
// through a real OAuth+PKCE login -- see evidence/TICK-026/ for how this run's
// token was captured.
//
// Usage:
//   k6 run scripts/k6_performance_test.js \
//     -e VUS=20 -e DURATION=60s \
//     -e CHAT_BASE_URL=https://chat.localhost -e COOKIE=ai_session=<value> \
//     -e EMR_BASE_URL=https://emr.localhost -e ACCESS_TOKEN=<bearer token>
//
// Any scenario whose credential env var is unset is skipped, not failed, so this
// script also runs a scheduling-only or chat-only pass.

import http from "k6/http";
import { check } from "k6";

const VUS = parseInt(__ENV.VUS || "20", 10);
const DURATION = __ENV.DURATION || "60s";
const CHAT_BASE_URL = __ENV.CHAT_BASE_URL || "https://chat.localhost";
const EMR_BASE_URL = __ENV.EMR_BASE_URL || "https://emr.localhost";
const COOKIE = __ENV.COOKIE || "";
const ACCESS_TOKEN = __ENV.ACCESS_TOKEN || "";

const scenarios = {};
if (COOKIE) {
  scenarios.chat_api = {
    executor: "constant-vus",
    vus: VUS,
    duration: DURATION,
    exec: "chatApi",
  };
}
if (ACCESS_TOKEN) {
  scenarios.scheduling_read = {
    executor: "constant-vus",
    vus: VUS,
    duration: DURATION,
    exec: "schedulingRead",
  };
  scenarios.scheduling_write = {
    executor: "constant-vus",
    vus: VUS,
    duration: DURATION,
    exec: "schedulingWrite",
  };
}

export const options = {
  scenarios,
  insecureSkipTLSVerify: true, // local Caddy-issued dev CA, not for production runs
  thresholds: {
    ...(COOKIE ? { "http_req_duration{scenario:chat_api}": ["p(95)<3000"] } : {}),
    ...(ACCESS_TOKEN
      ? {
          "http_req_duration{scenario:scheduling_read}": ["p(95)<1000"],
          "http_req_duration{scenario:scheduling_write}": ["p(95)<1000"],
        }
      : {}),
  },
};

// NFR-13: chat/API, <3.0s p95. A scheduling-style message stays on ChatService's
// path (never onboarding, which has no comparable performance target here).
export function chatApi() {
  const res = http.post(
    `${CHAT_BASE_URL}/api/chat`,
    JSON.stringify({ message: "What are my upcoming appointments?" }),
    {
      headers: {
        "Content-Type": "application/json",
        Origin: CHAT_BASE_URL,
        Cookie: COOKIE,
      },
      tags: { scenario: "chat_api" },
    }
  );
  check(res, { "chat/api 200": (r) => r.status === 200 });
}

// NFR-14 (read half): OpenEMR FHIR appointment read, independent of the chat/LLM
// layer entirely.
export function schedulingRead() {
  const res = http.get(`${EMR_BASE_URL}/apis/default/fhir/Appointment`, {
    headers: { Authorization: `Bearer ${ACCESS_TOKEN}` },
    tags: { scenario: "scheduling_read" },
  });
  check(res, { "scheduling read 200": (r) => r.status === 200 });
}

// NFR-14 (write half): a real book (TICK-040's module route) immediately followed
// by a cancel of the same appointment (TICK-036/041's route), so the write path is
// genuinely exercised each iteration without leaving orphaned synthetic
// appointments behind. Category/facility/billing-location ids match this
// deployment's own admin-configured AI_BOOKING_* values (deploy/local/.env) --
// override via env vars for a different environment.
const PC_CATID = parseInt(__ENV.PC_CATID || "9", 10);
const PC_FACILITY = parseInt(__ENV.PC_FACILITY || "3", 10);
const PC_BILLING_LOCATION = parseInt(__ENV.PC_BILLING_LOCATION || "3", 10);

export function schedulingWrite() {
  const bookRes = http.post(
    `${EMR_BASE_URL}/apis/default/portal/patient/appointment`,
    JSON.stringify({
      pc_catid: PC_CATID,
      pc_duration: 900,
      pc_facility: PC_FACILITY,
      pc_billing_location: PC_BILLING_LOCATION,
      pc_title: "AI-scheduled visit",
      pc_eventDate: "2026-12-01",
      pc_startTime: "09:00:00",
    }),
    {
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${ACCESS_TOKEN}` },
      tags: { scenario: "scheduling_write" },
    }
  );
  check(bookRes, { "scheduling book 201": (r) => r.status === 201 });
  if (bookRes.status !== 201) {
    return;
  }
  const auuid = JSON.parse(bookRes.body).id;
  const cancelRes = http.put(
    `${EMR_BASE_URL}/apis/default/portal/patient/appointment/${auuid}`,
    null,
    {
      headers: { Authorization: `Bearer ${ACCESS_TOKEN}` },
      tags: { scenario: "scheduling_write" },
    }
  );
  check(cancelRes, { "scheduling cancel 200": (r) => r.status === 200 });
}
