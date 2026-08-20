"""Prove the new patient-writable assessment-draft endpoint (TICK-017) is correctly
patient-bound, the same way TICK-028's probe proved (or disproved) it for the FHIR
Patient write route.

Non-interactive: drives the portal login the way evidence/TICK-002/PORTAL_HOOK_EVIDENCE.md
did (a plain cookie-jar form POST, no JavaScript involved), then reuses that session's
cookies on the OAuth `/authorize` request so the already-authenticated patient never sees
a login prompt. The registered client's "Patient standalone apps Auto Approved" setting
(recorded in evidence/TICK-002) means no manual consent step either, so the whole
authorization_code+PKCE dance completes with zero interactive/browser steps -- capture the
`Location` header of the final redirect directly instead of running a callback listener.

Stdlib only. Credentials are read from the environment, never written to a file, and
never printed.
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
CLIENT_ID = os.environ["TICK017_CLIENT_ID"]
CLIENT_SECRET = os.environ["TICK017_CLIENT_SECRET"]
REDIRECT_URI = "http://localhost:8910/callback"
SCOPE = (
    "openid fhirUser offline_access api:oemr api:fhir api:port "
    "patient/assessment.c patient/assessment.r patient/assessment.u"
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
    """Build the POST that approves every scope on OpenEMR's consent screen."""
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

    # Step 1: start the authorize request. For an unauthenticated session this lands
    # (after OpenEMR's own internal redirects) on the OAuth2 provider's own login
    # form at /oauth2/default/provider/login -- a distinct login flow from the
    # classic portal form, requiring a per-page CSRF token.
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

    def _follow(location: str, *, method: str = "GET", data: bytes | None = None,
                headers: dict | None = None) -> tuple[str, bytes]:
        """Issue one request, return (next_location_or_empty, body). Never auto-follows."""
        req = urllib.request.Request(location, data=data, headers=headers or {}, method=method)
        try:
            resp = opener.open(req, timeout=30)
            return "", resp.read()  # 200: no further redirect
        except urllib.error.HTTPError as exc:
            return exc.headers.get("Location", ""), exc.read()

    location, body = _follow(authorize_url)
    if not location and b"provider/login" not in body and b"name=\"username\"" not in body:
        raise ProbeError("authorize did not redirect and did not show a login form")
    if location:
        if location.startswith("/"):
            location = BASE + location
        location, body = _follow(location)

    # Step 2: parse the CSRF token off the login form and submit patient credentials.
    match = re.search(rb'name="csrf_token_form"\s+value="([^"]+)"', body)
    if not match:
        raise ProbeError("could not find csrf_token_form on the OAuth2 login page")
    csrf_token = match.group(1).decode()
    login_body = urllib.parse.urlencode(
        {
            "csrf_token_form": csrf_token,
            "username": username,
            "password": password,
            "user_role": "portal-api",  # the "Patient Login" submit button's value
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

    # Step 3: follow whatever OpenEMR redirects through next. Auto Approved skips a
    # repeat consent screen for a scope set already granted before, but the first
    # grant of a brand-new resource scope (patient/assessment.*, never approved by
    # this client before) still shows the "OpenEMR Authorization" consent screen --
    # submit it once, approving every offered scope, exactly as a real patient
    # clicking "Authorize" with every box checked (the default state) would.
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
            debug_path = os.environ.get("TICK017_DEBUG_DUMP")
            if debug_path:
                with open(debug_path, "wb") as fh:
                    fh.write(body)
            raise ProbeError(
                f"redirect chain stopped short of {REDIRECT_URI} with a 200 page "
                f"(body len={len(body)})"
            )
    if not code:
        raise ProbeError(f"authorize did not reach {REDIRECT_URI} within redirect budget")

    # Step 4: exchange the code for a token, same as probe_patient_context.py.
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
        payload = json.loads(resp.read())
    return payload


def api(
    method: str, path: str, token: str, body: dict | None = None, raw_body: bytes | None = None
) -> tuple[int, dict]:
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
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


def _check(results: list, name: str, status: int, expected: int, ok: bool, body: dict) -> None:
    if status == expected and ok:
        verdict = f"{expected} as expected"
    else:
        verdict = f"UNEXPECTED: HTTP {status} {body}"
    results.append((name, status, verdict))


def main() -> int:
    username_a = os.environ["TICK017_USER_A"]
    password_a = os.environ["TICK017_PW1"]
    username_b = os.environ["TICK017_USER_B"]
    password_b = os.environ["TICK017_PW2"]

    print(">>> Logging in patient A and obtaining a patient-context token...")
    token_a = login_and_get_token(username_a, password_a)["access_token"]
    print(">>> Logging in patient B and obtaining a patient-context token...")
    token_b = login_and_get_token(username_b, password_b)["access_token"]

    results: list = []

    # 1. Malformed JSON body must 400, not be silently treated as an empty draft.
    status, body = api(
        "POST", "/portal/patient/assessment", token_a, raw_body=b"{not valid json,,,"
    )
    _check(results, "A submits malformed JSON (must fail)", status, 400, True, body)

    # 2. Patient A creates a draft.
    status, body = api("POST", "/portal/patient/assessment", token_a, {"help_type": "not_sure_yet"})
    _check(results, "A creates own draft", status, 201, True, body)
    draft_uuid = body.get("uuid")
    if not draft_uuid:
        print("FATAL: create did not return a uuid:", body)
        return 1
    path = f"/portal/patient/assessment/{draft_uuid}"

    # 3. Patient A reads it back.
    status, body = api("GET", path, token_a)
    help_type_present = body.get("fields", {}).get("help_type") == "not_sure_yet"
    _check(results, "A reads own draft", status, 200, help_type_present, body)

    # 4. Patient A updates it incrementally.
    contact_fields = {"preferred_contact_method": "email", "contact_value": "avery@example.invalid"}
    status, body = api("PUT", path, token_a, contact_fields)
    fields = body.get("fields", {})
    both_present = (
        fields.get("help_type") == "not_sure_yet"
        and fields.get("preferred_contact_method") == "email"
    )
    _check(results, "A checkpoints a new field", status, 200, both_present, body)

    # 5. NEGATIVE: patient B attempts to read patient A's draft.
    status, body = api("GET", path, token_b)
    _check(results, "B reads A's draft (must fail)", status, 404, True, body)

    # 6. NEGATIVE: patient B attempts to write patient A's draft.
    status, body = api("PUT", path, token_b, {"help_type": "both"})
    _check(results, "B writes A's draft (must fail)", status, 404, True, body)

    # 7. Validation: reject an invalid enum value.
    invalid = {"help_type": "not-a-real-option"}
    status, body = api("POST", "/portal/patient/assessment", token_a, invalid)
    _check(results, "A submits invalid help_type (must fail)", status, 400, True, body)

    # 8. Completion requires all required fields; attempt with only 2 of 4 present.
    status, body = api("PUT", path, token_a, {"status": "completed"})
    _check(results, "A completes with missing required fields (must fail)", status, 400, True, body)

    # 9. Fill remaining required fields and complete for real.
    status, body = api(
        "PUT", path, token_a,
        {"visit_format": "video", "visit_time_window": "no_preference", "status": "completed"},
    )
    completed = body.get("status") == "completed"
    _check(results, "A completes with all required fields", status, 200, completed, body)

    # 10. Completed drafts are immutable.
    status, body = api("PUT", path, token_a, {"help_type": "both"})
    _check(results, "A edits a completed draft (must fail)", status, 409, True, body)

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
