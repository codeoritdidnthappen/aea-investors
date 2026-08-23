# TICK-057 verification — 2026-08-22

## The fault, as found

`docker inspect local-openemr-1` reported all three bind mounts sourced from
`/host_mnt/Users/megalodon/src/aea-investors/.builder/worktrees/TICK-051/...`.
`git worktree list` showed only the main checkout and `.builder/worktrees/` was
empty, so every source had been deleted under the running container.

Observed inside the container before the fix:

```
$ ls -R .../custom_modules/aeai-portal-chat
.../aeai-portal-chat:
$                                   # empty: no Bootstrap.php, no src/

$ wc -c .../templates/oauth2/oauth2-login.html.twig
0
$ wc -c .../templates/oauth2/scope-authorize.html.twig
0
```

`docker ps` reported the container **healthy** throughout. Nothing was logged.

The module being absent means `renderCard()` and `render()` never run, so no
tile, no panel and no iframe exist on the page — the chat is unreachable before
any JavaScript is involved. This is not TICK-054's deferred launch failing to
promote `data-src`; there is no element to promote.

Recreating the stack was itself blocked:

```
$ docker compose config --quiet
error while interpolating services.ai-server.environment.AI_SESSION_DASHBOARD_REDIRECT_URI:
required variable AI_SESSION_DASHBOARD_REDIRECT_URI is missing a value
```

TICK-051's rename guard behaving exactly as designed, against a gitignored `.env`
that no commit could reach.

## After the fix

| Check | Result |
|---|---|
| Worktree-sourced mounts on `local-openemr-1` | 0 |
| Bind mounts remaining | 1 (the module directory only) |
| `PortalChatController.php` host vs container | sha256 match |
| `oauth2-login.html.twig` baked into image, vs host | sha256 match |
| `scope-authorize.html.twig` baked into image, vs host | sha256 match |
| OpenEMR container health | `healthy` |
| `GET /health` | all four dependencies `ok` |
| `GET /oauth/launch` | `302` to the real authorize URL, PKCE + patient scopes |
| `preflight_local_stack.py` | `PREFLIGHT_OK` |
| Gate | 598 passed, 4 skipped, 92.69% coverage, ruff clean |

Healthcheck proven to discriminate, not merely present:

```
$ docker inspect --format '{{json .Config.Healthcheck.Test}}' local-openemr-1
["CMD-SHELL","test -s .../aeai-portal-chat/src/Controller/PortalChatController.php && curl --fail ... /readyz"]

$ docker exec local-openemr-1 sh -c 'test -s .../PortalChatController.php && echo present'
present
$ docker exec local-openemr-1 sh -c 'test -s .../NOPE.php || echo "fails as intended"'
fails as intended
```

The preflight found a second, unrelated drift on its first run: three keys
declared in `.env.example` were absent from `.env`. All three are referenced by
Compose with `:-` defaults whose values match `.env.example` exactly, so the
running stack was correct and this was **not** a live defect. The check was
narrowed accordingly — it now reports only keys Compose references bare, since a
check that fires on healthy configuration gets ignored.

## Not verified here

**The closing acceptance criterion — a patient signing in, seeing the tile, and
opening the chat — was not exercised.** It needs portal credentials for a seeded
synthetic patient, which this pass did not have. Everything up to the OAuth
handoff is verified above, and the deferred-launch code is confirmed byte-identical
inside the container, but the final user-visible step is unproven and the ticket
should not be closed on this record alone.
