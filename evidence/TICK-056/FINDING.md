# TICK-056 — OpenEMR 8.3.0 will not accept a patient-portal session at its OAuth2 authorization endpoint

**Executed:** 2026-08-23, against the pinned release (`FROM openemr/openemr:8.3.0`,
`deploy/local/openemr.Dockerfile:3`) running in the local Docker topology, driven
through a real patient-portal session for the seeded synthetic patient
`AverySubjecttest1` (pid 1).

**Answer: No — and the second sign-in is inherent to the release, not to this
deployment's configuration.** Consent is a separate question with the same
answer: it is presented on every authorization and prior consent does not
suppress it.

This is a negative result, and it is a complete outcome. The constraint is now
recorded in [`ARCHITECTURE.md` §2.1](../../ARCHITECTURE.md) so nobody re-derives
it. No follow-up ticket is filed, because the ticket asks for one only if reuse
turns out to be possible.

## What is in this directory

| File | What it is |
|---|---|
| `FINDING.md` | This document — the decision and its evidence. |
| `run_spike.sh` | The re-runnable harness. Seven experiments (E1–E7), each printing PASS/FAIL. Registers and then revokes its own probe client; reverts every mutation on exit. |
| `run_spike_output.txt` | Captured output of the run this document describes. |

Run it with `bash evidence/TICK-056/run_spike.sh` from the repo root, with the
local stack up per `deploy/local/README.md`.

## The question, precisely

A patient signed in to the portal opens the AI Chat panel. The panel's
`/oauth/launch` redirects to `/oauth2/default/authorize`. Can that endpoint be
made to recognise the portal session the patient already has, for a
patient-scoped *confidential* client, instead of demanding a password again?

Two things are being asked, and they have to be answered separately:

1. **Authentication** — can the login be skipped?
2. **Consent** — if it could, would the consent screen still appear?

## Answer 1 — authentication cannot be reused

With a live portal session in the cookie jar, `GET /oauth2/default/authorize`
returns `307` to `/oauth2/default/provider/login`, and that page renders a
password field (`run_spike.sh` E2). That holds with every configuration lever
this release exposes turned in the direction that would help.

The mechanism is not cookie scoping, which is the first thing one would suspect
and the first thing ruled out. OpenEMR issues four separate session cookies —
`OpenEMR` (core), `PortalOpenEMR` (portal), `authserverOpenEMR` (oauth2), and
`apiOpenEMR` (api) — defined at `src/Common/Session/SessionUtil.php:81-86` and
configured at `src/Common/Session/SessionConfigurationBuilder.php:15-50`. The
portal cookie is issued with `path=/`, so the browser *does* send it to
`/oauth2/default/authorize`. E2 prints the outgoing `Cookie:` header to prove
it. The cookie arrives; nothing on the server reads it.

What the server reads instead is the *core* session. The only code path in the
release that can skip the login is the SMART EHR-launch path:

- `src/RestControllers/AuthorizationController.php:642-646` — if a `launch`
  parameter is present *and* `shouldSkipAuthorizationFlow()` agrees, resolve a
  user via `getLoggedInCoreUserUuid()` and, if one is found, complete the
  authorization without a login.
- `:1730-1748` — `shouldSkipAuthorizationFlow()` requires the global
  `oauth_ehr_launch_authorization_flow_skip` to be `1` *and* the client's
  `skip_ehr_launch_authorization_flow` column to be set.
- `:1891-1939` — `getLoggedInCoreUserUuid()` reads the `OpenEMR` cookie
  (`SessionUtil::CORE_SESSION_ID`, `:1895`), loads it as a **core** session,
  and looks for `authUserID` (`:1915`). It then resolves that id through
  `UserService::getUser()`, i.e. against the `users` table.

A patient-portal session has no `authUserID`. E1 dumps the live session's keys:

```
authUser  csrf_private_key  enable_database_connection_pooling  landOn
language_choice  language_direction  patient_portal_onsite_two  pid
portal_login_username  portal_username  providerId  providerName
providerUName  ptName  sessionUser  site_id
```

It carries `pid` and `portal_username`; the nearest thing to a user id is
`authUser`, whose value is the literal string `portal-user`. There is no
`authUserID`, and a portal patient has no row in `users` to point one at.

The release says so itself, in a comment immediately above the lookup
(`AuthorizationController.php:1921`):

> `// for now we only handle in-ehr launch for providers not patients.  We can add this later if needed.`

## What was tried, and what each produced

A negative result is only worth having if it says what was ruled out.

| Lever | What was tried | Result |
|---|---|---|
| Cookie/session scoping | Sent the live `PortalOpenEMR` cookie to `/oauth2/default/authorize`; inspected issued cookie attributes and the four session configurations | Cookie **is** delivered (`path=/`). Ignored. Not a scoping problem. |
| Forcing the session across | Presented the portal session id as the core `OpenEMR` cookie | Still the login form. The core loader finds no `authUserID` in a portal session. |
| Global `oauth_ehr_launch_authorization_flow_skip` | Already `1` in this deployment — read, never written | Necessary, nowhere near sufficient. |
| Per-client `skip_ehr_launch_authorization_flow` | Set to `1` on a purpose-registered confidential client | Necessary, still not sufficient for a patient. |
| SMART `launch` + `aud` | Minted a real encrypted `SMARTLaunchToken` for pid 1 and sent both parameters with the portal session | `307` to `/oauth2/default/provider/login`. |
| Global `smart_context_test_launches` | Inspected; off. Previously assessed and rejected in `tickets/TICK-002-select-portal-hook.md:61-64` | Governs provider-side context-test launches, not patient auth. |
| `oauth_app_manual_approval` | `0` ("Patient standalone apps Auto Approved") | Governs whether a client is auto-*enabled*, not whether consent is shown. |
| Prior consent (`oauth_trusted_user`) | A row already existed for this patient and client | Consent still shown. See Answer 2. |
| The OAuth2 provider's own session | Completed a login, then started a second authorization on the same jar | Prompted again. See below — there is no already-authenticated check at all. |
| The release's own code paths | Read `AuthorizationController`, `SessionUtil`, `SessionConfigurationBuilder`, `HttpSessionFactory`, `ClientEntity` in the running container | Confirms the above and states the provider-only limitation outright. |

## The control that makes the negative credible

A negative that merely fails to happen can always be a broken test. So the same
request was run again with one variable changed: a synthetic **core** session
naming an existing `users` row (`users.id=3`, `portal-user`) instead of the
patient's portal session. Same URL, same client, same launch token, same
globals.

That request returned `200` with the EHR-launch autosubmit page, and on
autosubmit reached `https://chat.localhost/oauth/callback?code=…` — **no login,
no consent** (E4). The skip machinery is live, correctly configured, and works
in this deployment. It simply requires a `users` row, which a patient does not
have and cannot be given without inventing one.

No credential was used to build that session; a session file was written
directly. It is a diagnostic, never a product path.

## A correction to `ARCHITECTURE.md` §2.1 that fell out of this

§2.1 justified ADR-8's position-not-intent rule by asserting that "a live OAuth2 provider
session completes the whole exchange with no prompt at all". That is not true of 8.3.0.

`oauthAuthorizationFlow()` redirects to `/oauth2/default/provider/login`
*unconditionally* whenever the provider form is enabled
(`AuthorizationController.php:649-656`), and `userLogin()` renders a blank login form when
no credentials are posted. There is no already-authenticated check anywhere in the
authorization flow. E7 confirms it live: complete a login, hold the resulting
`authserverOpenEMR` session, start a second authorization on the same cookie jar — `307`
to the login form, password field and all.

The ADR-8 rule itself is unaffected and stays exactly as it was. It is stated over
position, and position is still the only thing the callback can know. Only the supporting
example was wrong; the EHR-launch skip path (E4) is a real prompt-less path and now
carries that sentence instead. §2.1 has been corrected accordingly.

## Answer 2 — consent is separate, and also does not persist

Consent is not merely coupled to the login; it is unconditional.

After a successful login, `userLogin()` always redirects to
`/scope-authorize-confirm` (`AuthorizationController.php:980`), with no lookup
of prior approval. `trustedUser()` — the function that reads `oauth_trusted_user`
— is declared at `:1475` and has exactly one call site in the whole release,
`:1523`, inside `userSessionLogout()`. It is never consulted before showing
consent. The `persist_login` flag ("remember me", set at `:970`) is written to
that table and read back only during logout and token exchange, never to
suppress the consent screen.

Live confirmation (E6): the product client already had an `oauth_trusted_user`
row for this patient. Two consecutive authorizations, both with correct
credentials, both produced the login form *and* then a consent page — byte-for-byte
identical at 36,592 bytes each time.

So: even in the hypothetical where the login were reusable, the patient would
still meet the consent screen on every single chat launch. That is the answer to
the ticket's third criterion, stated on its own.

The one path that does skip consent is the EHR-launch skip path from E4 — and it
skips consent precisely because it never asks a human anything. It sets
`persist_login = 0` and grants the client's registered scopes wholesale
(`:1800-1812`).

## What reuse would cost, if someone forced it

The ticket asks this only if reuse is possible. It is not — but the control
experiment answers it anyway, and the answer is worth recording, because the
EHR-launch path is the obvious thing for a future reader to reach for.

E5 exchanged the code that path issued. The `id_token`'s `sub` resolves to
`users.username = portal-user` — a **staff** row — while the `patient` claim
carries the patient's UUID, taken solely from the launch token
(`AuthorizationController.php:1804-1807`). The identity that authenticated is
not the identity the token grants access to. Nothing in that path checks that
the session's user *is* the patient in the launch token.

That is exactly the boundary TICK-028 established between a patient's portal
session and delegated API authorization. Adopting the EHR-launch skip path for
patients would dissolve it: whoever holds a core session mints patient-scoped
tokens, and the patient's own authentication stops being what authorizes access
to the patient's own chart. This is not a change to make casually, and it is not
in scope here.

## Deviations on the record

1. **The portal password for pid 1 was reset by SQL** immediately before the
   run, following the established convention
   (`evidence/TICK-055/run_live_verification.sh:59-68`). The password is not
   stored anywhere in the repo. Override with `TICK056_PORTAL_PASS`.
2. **A probe OAuth client was registered** through
   `POST /oauth2/default/registration` and given
   `skip_ehr_launch_authorization_flow = 1`. The harness revokes and disables it
   and deletes its `oauth_trusted_user` rows on exit. The product client was
   never modified.
3. **A synthetic core session file was written** into the openemr container's
   `/tmp` for the E4 control and removed on exit. No staff or admin credential
   was used at any point, in any path.
4. **`system_error_logging` was briefly set to `DEBUG`** during investigation to
   read the authorization server's decision trace, and restored to `WARNING`.
   The committed harness does not touch it.
5. **The flows were driven with `curl`, not a browser.** This is sound for the
   question asked — whether the *server* accepts a session — because the whole
   question is which cookie the server reads. It is not sound for anything about
   consent-form submission, which needs the form's own JavaScript
   (`evidence/TICK-045/FIX_VERIFICATION.md:135-163`); no such claim is made here.

## What this does NOT prove

- Nothing about OpenEMR versions other than 8.3.0. The comment at `:1921` reads
  like an acknowledged gap, so a later release may close it. Re-run this harness
  against any upgrade before assuming the constraint still holds.
- Nothing about whether the EHR-launch skip path is *safe* for providers. It was
  exercised only to establish that the machinery works.
- Nothing about patching or forking the vendor tree, which is out of scope.
- No claim that consent *should* be removed. That is a product and privacy
  decision, explicitly out of scope for this ticket.

## The patient's best achievable experience, given the second sign-in stays

The second sign-in cannot be removed within this release without modifying the
vendor tree. What is left is making it cost as little as possible, and the work
already merged does most of it:

1. **It must never ambush the patient.** TICK-054 made the panel authorize only
   when the tile is opened, so a patient who never opens the chat never sees a
   sign-in, and one who does has just clicked something.
2. **It must end where the patient was.** TICK-051 and ADR-8 land a completed
   authorization on the portal dashboard rather than a full-page chat, so the
   patient is one click from the chat with the portal still around them.
3. **It should happen once per session, not once per open.** `/oauth/launch`
   short-circuits on a live `ai_session` (`ARCHITECTURE.md` §2.1, mechanic 2),
   so the cost is paid on the first chat open of a session and not again.

That reduces the patient's experience to: open the chat, sign in once more,
consent once, land back on the dashboard, open the chat again — and thereafter,
for the life of the session, the chat opens directly. The remaining friction is
the one-time round trip, and within OpenEMR 8.3.0 that is the floor.

Whether that floor is acceptable for FR-2 is a product judgement, not a
technical one, and it now has the numbers behind it.
