---
id: TICK-056
title: "spike(auth): determine whether OpenEMR's OAuth2 provider can accept an existing portal session"
type: spike
epic: EPIC-04
priority: P2
estimate: M
depends_on: [TICK-028, TICK-054]
labels: [openemr, oauth, portal, discovery]
source: [FR-2, FR-3]
status: done
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/111
builder_commit: b3662d6
---
## Context

A patient who is already signed in to the OpenEMR patient portal is asked to
sign in a second time when they open the AI Chat panel. This is documented
behaviour, not a defect in our code: `evidence/TICK-024/DESKTOP_E2E_EVIDENCE.md:29-30`
records that `/oauth2/default/provider/login` requires "a fresh login there
(distinct from the portal session, matching the OAuth2 provider's own
session model)".

TICK-054 stops that second login from ambushing the patient on dashboard
render, and TICK-051 makes sure it ends on the dashboard rather than a
full-page chat. Neither removes the second login itself. The difference for
the patient is "click AI Chat and it opens" versus "click AI Chat, sign in
again, consent, come back, click AI Chat again" -- which is the whole
premise of FR-2, the chat being usable inside the OpenEMR frontend rather
than feeling like a separate application.

Nobody has established whether that second sign-in is inherent to OpenEMR
8.3.0 or an artefact of how this deployment is configured. It is worth
knowing before anyone builds around it, and worth knowing *before* the
onboarding flow depends on patients reaching the chat without friction.

This is a discovery ticket. Its deliverable is a decision with evidence, not
a code change. If the answer is "no", that is a complete and successful
outcome and the constraint gets recorded where the next person will find it.

## Acceptance Criteria

- [ ] A written finding states whether OpenEMR 8.3.0's OAuth2 authorization
      endpoint can be made to accept an authenticated patient-portal session
      in place of its own login, for a patient-scoped confidential client.
- [ ] The finding names what was actually tried -- configuration globals,
      SMART EHR-launch parameters (`launch`/`aud`), session or cookie
      scoping between the portal and the OAuth2 provider, and whatever the
      release's own code paths reveal -- and what each produced. A negative
      result is only credible if it says what was ruled out.
- [ ] Consent is treated separately from authentication. If the login can be
      reused but the consent screen still appears every time, that is stated
      as its own answer, along with whether prior consent persists for this
      client.
- [ ] Every claim rests on the pinned release running in the local Docker
      topology, exercised through a real patient portal session -- not on
      upstream documentation, forum posts, or reading source alone.
- [ ] If reuse is possible, the ticket ends with a follow-up ticket
      describing the change and its security implications, in particular
      whether it would weaken the boundary TICK-028 established between the
      patient's portal session and delegated API authorization.
- [ ] If reuse is not possible, the constraint is recorded in
      `ARCHITECTURE.md` §2.1 so the next person does not re-derive it, and
      the ticket says what the patient's best achievable experience is
      given the second sign-in stays.

## Testing

Discovery, so the evidence *is* the deliverable. Record the attempts and
their outcomes under `evidence/TICK-056/`, following the standard set by
`evidence/TICK-028/` and `evidence/TICK-024/`: real seeded synthetic
patients, the pinned container, and no admin credential in any product path.
No production code is expected to change; if it does, that lands in its own
ticket rather than here.

## Out of Scope

Implementing single sign-on, modifying OpenEMR's authorization server, or
patching the vendor tree. Removing or weakening the consent step, which is a
product and privacy decision rather than a technical one. The absent logout
path (TICK-055).
