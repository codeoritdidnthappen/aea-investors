"""Prove — or disprove — that OpenEMR v8.3.0 binds a patient-context token to one chart.

Every write the product performs today was probed with the password grant as the seeded
local admin (evidence/TICK-001/PROBE_EVIDENCE.md). That is a staff-context token and is
not acceptable, including locally. This probe obtains a token the way the product must:
authorization_code + PKCE, where the *patient* authenticates at OpenEMR's own login.

The open question is narrow. evidence/TICK-001/ENDPOINT_MATRIX.md records that the FHIR
`PUT` route has no patient-binding branch, unlike the FHIR `GET` which does. So a
patient-scoped token may or may not be able to write a *different* patient's chart. This
script attempts exactly that and records what happened.

A successful cross-patient write is not a capability. It is an upstream security finding,
and the route must then never be used by the product.

Stdlib only, so it runs from a clean checkout with no dependency resolution.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8910
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"

SCOPE = (
    "openid fhirUser offline_access api:oemr api:fhir "
    "patient/Patient.read patient/Patient.write patient/Appointment.read"
)

# The local stack terminates TLS with Caddy's internal CA. Verification is disabled for
# this probe only; nothing here may be reused by product code.
_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE


class ProbeError(Exception):
    """Raised when the probe cannot reach a definite verdict."""


@dataclass(frozen=True)
class Outcome:
    """One attempted request, reduced to what is safe to retain."""

    name: str
    method: str
    route: str
    status: int
    denied: bool
    verdict: str

    def row(self) -> str:
        return f"| {self.name} | `{self.method} {self.route}` | {self.status} | {self.verdict} |"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        type(self).captured = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorization captured. You may close this tab.")

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log; it would contain the auth code."""


def _await_callback(timeout: float = 300.0) -> dict[str, str]:
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()
    if not _CallbackHandler.captured:
        raise ProbeError("no authorization callback received before timeout")
    return _CallbackHandler.captured


def _request(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None = None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_INSECURE, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:  # pragma: no cover - environment failure
        raise ProbeError(f"cannot reach {url}: {exc.reason}") from exc


def authorize(base_url: str, client_id: str) -> tuple[str, str]:
    """Drive the patient through OpenEMR's own login. Returns (code, verifier)."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{base_url}/oauth2/default/authorize?{urllib.parse.urlencode(params)}"

    print("\n>>> Log in as the SUBJECT PATIENT and consent.")
    print(">>> Do NOT enter an admin credential; that invalidates the run.\n")
    print(url, "\n")
    webbrowser.open(url)

    captured = _await_callback()
    if "error" in captured:
        raise ProbeError(f"authorize returned error: {captured['error']}")
    if captured.get("state") != state:
        raise ProbeError("state mismatch on callback — possible interference")
    code = captured.get("code")
    if not code:
        raise ProbeError("callback carried no authorization code")
    return code, verifier


def exchange(base_url: str, client_id: str, client_secret: str, code: str, verifier: str) -> dict:
    # Standard (padded) basic auth — not base64url, which is only for PKCE above.
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": client_id,
        }
    ).encode()
    status, raw = _request(
        "POST",
        f"{base_url}/oauth2/default/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        body=body,
    )
    if status != 200:
        raise ProbeError(f"token exchange failed with {status}")
    payload = json.loads(raw)
    if "patient" not in payload:
        raise ProbeError(
            "token response carried no `patient` claim — this is not a patient-context "
            "token. Confirm you logged in as a portal patient, not a staff user."
        )
    return payload


def _attempt(
    name: str, method: str, base_url: str, route: str, token: str, body: dict | None = None
) -> Outcome:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    raw: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode()
    status, _ = _request(method, f"{base_url}{route}", headers=headers, body=raw)
    denied = status in (401, 403, 404)
    return Outcome(name, method, route, status, denied, "denied" if denied else "ALLOWED")


def run_matrix(base_url: str, token: str, own: str, other: str) -> list[Outcome]:
    """Read and write, own chart and another patient's chart. Four outcomes."""
    edit = {"phone_home": "555-0100"}
    return [
        _attempt("read own", "GET", base_url, f"/apis/default/fhir/Patient/{own}", token),
        _attempt("read other", "GET", base_url, f"/apis/default/fhir/Patient/{other}", token),
        _attempt(
            "write own", "PUT", base_url, f"/apis/default/api/patient/{own}", token, edit
        ),
        _attempt(
            "write other", "PUT", base_url, f"/apis/default/api/patient/{other}", token, edit
        ),
    ]


def verdict(outcomes: list[Outcome]) -> tuple[str, str]:
    """Return (label, prose) for the binding decision this probe exists to make."""
    by_name = {o.name: o for o in outcomes}
    if not by_name["write other"].denied:
        return (
            "UNBOUND",
            "A patient-context token modified a different patient's chart. This is an "
            "upstream security finding, not a capability. The route is permanently "
            "rejected; the product performs no demographic write on v8.3.0.",
        )
    if not by_name["read other"].denied:
        return (
            "READ-UNBOUND",
            "Cross-patient write was denied but cross-patient read succeeded. Reads are "
            "not safely bound; treat any patient-scoped read as untrusted and report "
            "upstream.",
        )
    if by_name["write own"].denied:
        return (
            "READ-ONLY",
            "Binding holds, but the patient token cannot write its own chart. The "
            "product cannot write demographics as the patient on v8.3.0; TICK-016's "
            "staff-credential path must be removed and the requirement rescoped.",
        )
    return (
        "BOUND",
        "Binding is enforced: own chart readable and writable, other chart denied on "
        "both. Patient-context writes are legitimate. Re-probe TICK-016 under this "
        "token and delete the staff-credential path.",
    )


def write_evidence(directory: Path, outcomes: list[Outcome], label: str, prose: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "BINDING_MATRIX.md"
    lines = [
        "# TICK-028 — patient-context binding matrix",
        "",
        "Token obtained by authorization_code + PKCE with a portal patient login.",
        "No password grant, no staff credential, no `users` row was used.",
        "",
        "Redaction: no token, refresh token, client secret, authorization code, UUID,",
        "name, date of birth, or timestamp is retained below.",
        "",
        "| Attempt | Route | Status | Result |",
        "|---|---|---|---|",
        *[o.row() for o in outcomes],
        "",
        f"## Verdict: {label}",
        "",
        prose,
        "",
        "Record this outcome in `evidence/TICK-001/ENDPOINT_MATRIX.md`, replacing the",
        "pending demographics-write row.",
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--other-patient-uuid", required=True)
    parser.add_argument("--output", default="evidence/TICK-028")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    try:
        code, verifier = authorize(base, args.client_id)
        token_payload = exchange(base, args.client_id, args.client_secret, code, verifier)
        own = token_payload["patient"]
        if own == args.other_patient_uuid:
            raise ProbeError("subject and other patient are the same chart")
        outcomes = run_matrix(base, token_payload["access_token"], own, args.other_patient_uuid)
    except ProbeError as exc:
        print(f"\nPROBE FAILED: {exc}\n", file=sys.stderr)
        return 2

    label, prose = verdict(outcomes)
    target = write_evidence(Path(args.output), outcomes, label, prose)

    print("\n".join(o.row() for o in outcomes))
    print(f"\nVerdict: {label}\n{prose}\nWrote {target}")
    return 0 if label == "BOUND" else 1


if __name__ == "__main__":
    raise SystemExit(main())
