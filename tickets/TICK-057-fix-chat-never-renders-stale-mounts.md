---
id: TICK-057
title: "bug(deploy): the chat never renders -- the stack mounts a deleted build worktree and cannot be restarted"
type: task
epic: EPIC-08
priority: P1
estimate: M
depends_on: [TICK-022, TICK-051, TICK-054]
labels: [deploy, portal, chat, bug]
source: [FR-2, NFR-15]
status: in_progress
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/118
builder_commit: 9e3efe3
---
## Context

User report (2026-08-22): sign-in and navigation are noticeably better, but the
chat never comes up at all.

Diagnosed against the running stack, not inferred. Two independent defects,
both blocking, and the second is why the first cannot be worked around.

### 1. The container mounts a build worktree that no longer exists

`docker inspect local-openemr-1` reports the module bind mount as:

```
/host_mnt/Users/megalodon/src/aea-investors/.builder/worktrees/TICK-051/openemr_modules/aeai-portal-chat
  -> /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/aeai-portal-chat
```

Both twig overrides are mounted from that same path. `.builder/worktrees/TICK-051`
does not exist -- `git worktree list` shows only the main checkout, and
`.builder/worktrees/` is empty. build-agent ran the ticket in a worktree, brought
the stack up from inside it (so compose resolved `../../openemr_modules/...`
against the worktree), and the worktree was removed when the run finished.

The result inside the container:

```
$ ls -R .../custom_modules/aeai-portal-chat
.../aeai-portal-chat:
$                       # empty -- no Bootstrap.php, no src/, nothing
```

No module means no `renderCard()` and no `render()`, so there is no dashboard
tile, no accordion panel, and no iframe on the page at all. This is not
TICK-054's deferred launch failing to promote `data-src`; there is no element to
promote. The chat cannot appear under any interaction.

The twig overrides are mounted from the same dead path, so TICK-045's breakout
and TICK-047's shared script are equally absent from the running container.

### 2. The stack cannot be restarted to fix it

The obvious remedy -- recreate the stack from the real checkout -- fails before
it starts:

```
$ docker compose config --quiet
error while interpolating services.ai-server.environment.AI_SESSION_DASHBOARD_REDIRECT_URI:
required variable AI_SESSION_DASHBOARD_REDIRECT_URI is missing a value:
set AI_SESSION_DASHBOARD_REDIRECT_URI in deploy/local/.env
```

TICK-051 deliberately renamed `AI_SESSION_SUCCESS_REDIRECT_URI` into
`AI_SESSION_DASHBOARD_REDIRECT_URI` and `AI_SESSION_CHAT_ORIGIN` so a stale
deployment fails loudly instead of booting with the wrong destination. That
guard is working exactly as designed. But `deploy/local/.env` is gitignored and
was never updated, so it still carries only the old variable at line 27, and
`docker compose` refuses every subcommand.

The currently running `ai-server` container *does* have both new variables and
`/api/logout`, because the builder supplied them through its own shell
environment. That state is unreproducible from the repository: the moment the
container is recreated, it cannot start.

## Acceptance Criteria

- [ ] `deploy/local/.env` carries both renamed variables, and
      `docker compose config --quiet` exits clean from `deploy/local`. The
      `.env.example` already lists them; the gap is only in the untracked local
      file, so the fix includes whatever makes that gap visible next time
      (see the preflight criterion below).
- [ ] After recreating the stack from the repository root, the module directory
      inside the container is populated -- `Bootstrap.php` and
      `src/Controller/PortalChatController.php` are present and byte-identical
      to the host copies. Assert this, do not eyeball it.
- [ ] Both twig overrides in the running container are byte-identical to
      `openemr_overrides/templates/oauth2/*.twig`. They are mounted from the same
      dead path today and must be re-verified, not assumed.
- [ ] A patient signing in to the portal sees the AI Chat tile, and opening it
      loads the chat. This is the reported symptom and is the criterion that
      actually closes the ticket.
- [ ] A **preflight** refuses to start a degraded stack, and is documented as the
      step before `docker compose up`. It fails on a bind-mount source that is
      missing or resolves empty, on a compose file inside a build worktree, and
      on a `.env` missing a variable Compose references without a default. It
      does **not** fire on `${VAR:?}` (Compose already refuses those with a clear
      message) or `${VAR:-default}` (optional by construction) -- a check that
      cries wolf gets ignored, and an ignored check protects nothing.
- [ ] A **healthcheck** covers the same fault continuously, because a mount can
      die underneath an already-running container -- which is what happened, with
      `docker ps` still reporting healthy. OpenEMR reports unhealthy when the
      module controller is absent or empty.
- [ ] The two OAuth template overrides are **copied in at build time**, not
      bind-mounted individually. A single-file mount whose source disappears
      leaves an empty file shadowing the vendor original rather than falling back
      to it, which blanked both OAuth pages here and went stale once before.
      OpenEMR therefore builds from its own Dockerfile over the pinned image.
- [ ] Starting the stack from a build worktree is refused outright by the
      preflight, so the cause is blocked rather than only detected.

## Verification status

Implementation is complete and recorded in
`evidence/TICK-057/VERIFICATION_2026-08-22.md`. Every criterion above is verified
except one: **a patient signing in, seeing the tile, and opening the chat.** That
needs portal credentials for a seeded synthetic patient, which the fixing pass did
not have. The ticket stays open on that criterion alone -- the repo's own rule is
that "done" means the change was seen working, and this step has not been.

## Testing

Recreate the stack from a clean checkout and assert, from outside the
container, that every bind-mount source exists and that each mounted file
matches its host original by checksum -- the failure this ticket exists for was
invisible from inside the application and only appeared under `docker inspect`.

Then live verification with real desktop Chrome, matching TICK-024's and
TICK-054's bar: sign in as a seeded synthetic patient, confirm the tile is
present without scrolling, open it, and hold a chat turn that streams a reply.
Then restart the stack and repeat, to prove the fix survives a recreate rather
than depending on a hand-fixed container. Record under `evidence/TICK-057/`.

Note the hazard already recorded for this repo, which this ticket is a direct
instance of: `ai-server` is not bind-mounted and needs `--build`, and the
single-file twig mounts go stale or truncate. Diff host against container
before trusting any verification result.

## Out of Scope

Changing the deferred-launch behaviour (TICK-054), the destination invariant
(TICK-051, ADR-8), or the logout path (TICK-055). None of them is implicated --
the module simply is not present in the container. Changing how build-agent
isolates its work, beyond stopping it from leaving a stack pointed at a
disposable path.
