"""Live cross-patient proof for the cancel-by-status parity claims in
`evidence/TICK-021/PARITY_MATRIX.md` (TICK-021), the same discipline
`scripts/probe_assessment_draft.py` used for TICK-017 and
`scripts/probe_patient_context.py` used for TICK-028.

`ai_server/tests/test_scheduling_parity.py` proves, with synthetic/mocked OpenEMR
responses, that `ai_server.scheduling.cancel.AppointmentCancelAdapter` relays
OpenEMR's cancel-by-status outcome unchanged. This script proves the *native* side of
that pairing is what the synthetic mocks assume: a real patient-context token can
cancel its own appointment, cannot cancel another patient's, and cannot cancel an
already-cancelled one, against the live local stack.

Not run by this build (Docker/a live OpenEMR stack is not available and starting one
is outside a build worker's scope) -- see `evidence/TICK-021/PARITY_MATRIX.md` for
what remains manual. An operator running this needs, ahead of time:

- Two synthetic patients with portal login credentials (patient A and B).
- At least one active (non-cancelled) appointment already booked for patient A --
  create it through OpenEMR's native calendar/portal UI, or through
  `ai_server.scheduling.booking` once wired, and note that "native" creation is
  itself part of what this script's cancel checks are being compared against.

Reuses the login flow `probe_assessment_draft.py` already validated (OpenEMR's own
OAuth2 login form, no JavaScript, no browser). Stdlib only; credentials are read from
the environment, never written to a file or printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://emr.localhost"
CLIENT_ID = os.environ["TICK021_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICK021_CLIENT_SECRET"]
REDIRECT_URI = "http://localhost:8910/callback"
SCOPE = (
    "openid fhirUser offline_access api:oemr api:fhir api:port "
    "patient/Appointment.read patient/appointment.u"
)

_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE


class ProbeError(Exception):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # capture, don't follow


def _opener() -> urllib.request.OpenerDirector:
    cj_handler = urllib.request.HTTPCookieProcessor()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_INSECURE), cj_handler, _NoRedirect()
    )


def _consent_submission(body: bytes) -> tuple[str, dict]:
    action_match = re.search(rb'<form[^>]*\baction="([^"]+)"', body)
    if not action_match:
        raise ProbeError("consent screen had no form action")
    action = action_match.group(1).decode()
    csrf_match = re.search(rb'name="csrf_token_form"\s+value="([^"]+)"', body)
    if not csrf_match:
        raise ProbeError("consent screen had no csrf_token_form")
    fields = {"csrf_token_form": csrf_match.group(1).decode(), "proceed": "1"}
    for scope in re.findall(rb'name="scope\[([^\]]+)\]"', body):
        fields[f"scope[{scope.decode()}]"] = scope.decode()
    for resource, action_name in re.findall(
        rb'data-resource="([^"]+)"\s+data-action="([^"]+)"\s+value="[^"]*"\s+checked', body
    ):
        scope = f"patient/{resource.decode()}.{action_name.decode()}"
        fields[f"scope[{scope}]"] = scope
    data = urllib.parse.urlencode(fields).encode()
    url = action if action.startswith("http") else f"{BASE}{action}"
    return url, {
        "method": "POST",
        "data": data,
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
    }


def login_and_get_token(username: str, password: str) -> dict:
    opener = _opener()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{BASE}/oauth2/default/authorize?{urllib.parse.urlencode(params)}"

    def _follow(
        location: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict | None = None,
    ) -> tuple[str, bytes]:
        req = urllib.request.Request(location, data=data, headers=headers or {}, method=method)
        try:
            resp = opener.open(req, timeout=30)
            return "", resp.read()
        except urllib.error.HTTPError as exc:
            return exc.headers.get("Location", ""), exc.read()

    location, body = _follow(authorize_url)
    if not location and b"provider/login" not in body and b'name="username"' not in body:
        raise ProbeError("authorize did not redirect and did not show a login form")
    if location:
        if location.startswith("/"):
            location = BASE + location
        location, body = _follow(location)

    match = re.search(rb'name="csrf_token_form"\s+value="([^"]+)"', body)
    if not match:
        raise ProbeError("could not find csrf_token_form on the OAuth2 login page")
    csrf_token = match.group(1).decode()
    login_body = urllib.parse.urlencode(
        {
            "csrf_token_form": csrf_token,
            "username": username,
            "password": password,
            "user_role": "portal-api",
        }
    ).encode()
    location, body = _follow(
        f"{BASE}/oauth2/default/login",
        method="POST",
        data=login_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not location:
        raise ProbeError("login POST did not redirect -- credentials likely rejected")

    code = None
    for _ in range(6):
        if location.startswith("/"):
            location = BASE + location
        if location.startswith(REDIRECT_URI):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            if "error" in query:
                raise ProbeError(f"authorize returned error: {query['error']}")
            if query.get("state", [None])[0] != state:
                raise ProbeError("state mismatch")
            code = query.get("code", [None])[0]
            break
        location, body = _follow(location)
        if not location and b'name="proceed"' in body:
            url, kwargs = _consent_submission(body)
            location, body = _follow(url, **kwargs)
            continue
        if not location:
            raise ProbeError(
                f"redirect chain stopped short of {REDIRECT_URI} with a 200 page "
                f"(body len={len(body)})"
            )
    if not code:
        raise ProbeError(f"authorize did not reach {REDIRECT_URI} within redirect budget")

    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    token_body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
        }
    ).encode()
    token_req = urllib.request.Request(
        f"{BASE}/oauth2/default/token",
        data=token_body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    with urllib.request.urlopen(token_req, context=_INSECURE, timeout=30) as resp:
        return json.loads(resp.read())


def api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        f"{BASE}/apis/default{path}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, context=_INSECURE, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


def _check(results: list, name: str, status: int, expected: int, body: dict) -> None:
    verdict = (
        f"{expected} as expected" if status == expected else f"UNEXPECTED: HTTP {status} {body}"
    )
    results.append((name, status, verdict))


def main() -> int:
    username_a = os.environ["TICK021_USER_A"]
    password_a = os.environ["TICK021_PW_A"]
    username_b = os.environ["TICK021_USER_B"]
    password_b = os.environ["TICK021_PW_B"]

    print(">>> Logging in patient A and obtaining a patient-context token...")
    token_a = login_and_get_token(username_a, password_a)["access_token"]
    print(">>> Logging in patient B and obtaining a patient-context token...")
    token_b = login_and_get_token(username_b, password_b)["access_token"]

    status, body = api("GET", "/fhir/Appointment", token_a)
    entries = body.get("entry", []) if status == 200 else []
    active = next(
        (
            e["resource"]["id"]
            for e in entries
            if e.get("resource", {}).get("status") not in ("cancelled", None)
        ),
        None,
    )
    if not active:
        print(
            "FATAL: patient A has no active appointment to cancel. Book one first "
            "(native OpenEMR calendar/portal UI, or ai_server.scheduling.booking) "
            "and re-run.",
            file=sys.stderr,
        )
        return 1

    results: list = []
    path = f"/portal/patient/appointment/{active}"

    # NEGATIVE: patient B (not the owner) attempts to cancel patient A's appointment.
    status, body = api("PUT", path, token_b)
    _check(results, "B cancels A's appointment (must fail: not eligible)", status, 404, body)

    # Patient A cancels their own appointment.
    status, body = api("PUT", path, token_a)
    _check(results, "A cancels own appointment", status, 200, body)

    # Patient A attempts to cancel the same appointment again.
    status, body = api("PUT", path, token_a)
    _check(results, "A cancels an already-cancelled appointment (must fail)", status, 409, body)

    print("\n=== RESULTS ===")
    failed = False
    for name, status, verdict in results:
        marker = "FAIL" if "UNEXPECTED" in verdict else "ok"
        if marker == "FAIL":
            failed = True
        print(f"[{marker}] {name}: HTTP {status} -- {verdict}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
