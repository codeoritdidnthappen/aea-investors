# TICK-054 — the chat panel starts no authorization until the patient opens it

**Executed:** 2026-08-22, real desktop Chrome (Google Chrome for Testing,
headless=new, driven over raw CDP) against the live local Docker topology —
Caddy + OpenEMR 8.3.0 + ai-server + MariaDB, all four containers up.

**Result: 22/22 checks pass.** Every acceptance criterion below is decided by a
network capture or by the ai-server's own request log, not by reading markup.

Reproduce: `bash evidence/TICK-054/run_live_verification.sh` from the worktree
root.

## What is in this directory

| File | What it is |
|---|---|
| `run_live_verification.sh` | The driver. Re-renders the panel from this worktree, resets the seeded patient's portal password, runs the capture, and diffs the ai-server's `/oauth/launch` hit count across the run. |
| `verify_deferred_chat_launch.mjs` | The capture itself: six cases over CDP `Network`/`Fetch`/`Input`. Exits non-zero on any failed check. Starts nothing that outlives it. |
| `render_panel_probe.php` | Executes this worktree's `PortalChatController` with the OpenEMR container's own PHP, so the harness can never verify hand-copied markup. |
| `rendered_panel.html` | That probe's output — the exact bytes the module will emit once this merges. |
| `screenshot-case*.png` | Captured at the end of each case. |

## Method, and the one substitution it makes

`local-openemr-1` bind-mounts the module from the **main checkout**, not from
this build worktree, so the running stack still serves the pre-TICK-054 panel
with a live `src`. Rather than mutate the container or the main checkout — the
mount hazard on the record in `evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md`
— the harness intercepts the real `portal/home.php` response and swaps the old
panel block for `rendered_panel.html`. Nothing else about the page, the session,
the OAuth chain or the ai-server is simulated.

That substitution is honest only because `rendered_panel.html` is produced by
running the worktree's controller through the container's PHP on every run
(`run_live_verification.sh` step 1, `php -l` first). The harness refuses to start
if that file has no `data-src`.

## Case by case

Login: seeded synthetic patient `AverySubjecttest1` (pid 1) through the classic
portal form at `https://emr.localhost/portal/index.php?site=default`, landing on
`portal/home.php`. No admin credential is used anywhere.

### 1. Dashboard render — AC1, AC5

No AI session cookie exists in the fresh browser profile, which *is* the
"AI session expired" condition the ticket asks for: `/oauth/launch` has no
short-circuit yet (that is TICK-051's), so any request would have started a full
authorization.

```
PASS  AC1 dashboard render issued no request to the AI server -- 0 chat.localhost requests
PASS  panel renders with no src attribute -- src=null
PASS  panel carries the launch URL in data-src only -- data-src=https://chat.localhost/oauth/launch
PASS  patient was not moved off the dashboard
PASS  AC5 tile and accordion grouping unchanged -- {"tileToggle":"collapse","tileParent":"#cardgroup","panelParent":"#cardgroup"}
```

Screenshot: `screenshot-case1-dashboard-render.png`.

### 2. The patient opens the tile with the mouse — AC2

```
PASS  the click promoted data-src to src -- src=https://chat.localhost/oauth/launch
PASS  AC2 opening the tile starts exactly one authorization -- 1 requests to https://chat.localhost/oauth/launch
PASS  the deferred launch is a real authorization (it reaches OpenEMR) -- https://emr.localhost/oauth2/default/authorize?response_type=code&client_id=xi5CDW1y7OWLMB...
    top-level after opening: https://emr.localhost/oauth2/default/provider/login
```

The last line is the *correct* behaviour and is out of scope here: the launch
reaches OpenEMR's authorization server, which demands its own login, and
TICK-045's breakout hoists that login to the top level. Where the patient lands
after signing in there is TICK-051's. What this ticket changes is that the flow
now begins on the patient's click and not on the dashboard painting.

That hoist happens within ~200ms, taking the panel's DOM with it, so the `src`
promotion is recorded by a `MutationObserver` writing to `sessionStorage` (same
origin as the breakout's destination) rather than sampled afterwards.

### 3. Dashboard reload after the chat has been used — AC1, AC5

This is the case a `show.bs.collapse`-only fix would have failed. `portal/home.php`
persists the last panel the patient used and re-opens it on every later dashboard
load (`let gowhere = {{ whereto | js_escape }}; $(gowhere).collapse('show');`,
`templates/portal/home.html.twig:318-319`), and the AI Chat tile sits inside
`#quickstart-card`, so clicking it does set `whereto` to `#aeai-portal-chat`.
The capture confirms `whereto=#aeai-portal-chat` on this load — the case really
was exercised, not merely present in theory.

```
PASS  AC1 reload after using the chat still issues no request to the AI server -- whereto=#aeai-portal-chat, 0 chat.localhost requests
PASS  the portal's own panel restore did not open the chat -- panel .show=false, whereto=#aeai-portal-chat
PASS  AC5 the cancelled restore still lands the patient on a usable dashboard -- #quickstart-card .show=true, tile visible=true
PASS  patient was not moved off the reloaded dashboard
```

`screenshot-case3-reload-after-use.png` shows the full tile grid with AI Chat in
it and no panel open — the same dashboard a patient who has never opened the
chat sees.

The second of those checks is why the implementation does more than cancel. An
earlier revision only called `event.preventDefault()`, and the live run caught
what static tests could not: the tile grid is *itself* a `.collapse` card, so
cancelling the restore left every card in `#cardgroup` closed and the dashboard
blank, with the AI Chat tile hidden inside it. That revision's screenshot is not
kept, but the failure was reproducible and is why
`deferredLoadScript()` falls back to opening `#quickstart-card`.

### 4. The patient opens the tile with the keyboard — AC2, NFR-19

Real `Input.dispatchKeyEvent` Enter on the focused tile anchor, with
`Emulation.setFocusEmulationEnabled` on (headless Chrome is not the OS focus
owner, so `element.focus()` is otherwise a no-op and the case would silently
measure nothing).

```
PASS  AC2 Enter on the focused tile starts the authorization (NFR-19) -- focus=aeai-chat-go, 1 requests to https://chat.localhost/oauth/launch
```

### 5. Collapse and re-open does not reload the frame — AC3

The route a patient actually takes: open AI Chat, press the portal's own
**Dashboard** button (the `#cardgroup` accordion collapses the chat when the chat
opens, so the tile is not on screen to click again), then open AI Chat once more.

```
PASS  AC1 holds with the launch response stubbed too -- 0 chat.localhost requests on render
PASS  first open loads the chat once -- 1 requests, shown=true
PASS  leaving the chat via the Dashboard button collapses the panel -- chat .show=false, #quickstart-card .show=true
PASS  AC3 re-opening the panel did not reload the iframe -- 1 requests to https://chat.localhost/oauth/launch across open -> collapse -> open
PASS  AC3 the panel is open again with the same src -- shown=true src=https://chat.localhost/oauth/launch
```

One request across open → collapse → open. A reload on every `show.bs.collapse`
would have produced three, and would have discarded the patient's transcript
twice (NFR-33 keeps it in the panel's DOM and nowhere else).

### 6. What the deferred load put in the DOM — AC4

```
PASS  AC4 no token or patient identifier entered the DOM -- panel subtree carries only the launch URL
PASS  AC4 the AI server is the panel subtree's only network target -- only chat.localhost
```

Every attribute of every element in the panel subtree was enumerated in the live
page; the only absolute URL among them is `https://chat.localhost/oauth/launch`.

## Server-side corroboration

The browser-side capture is not the only witness. The driver diffs the
ai-server's own request log across the run:

```
ai-server /oauth/launch hits: before=4 after=6 delta=2
```

Two, exactly as predicted before the run: one for the mouse open (case 2), one
for the keyboard open (case 4). Cases 1, 3 and 5's dashboard renders contributed
none. Case 5's single launch is stubbed at the browser and never leaves it.

## Deviations on the record

- **The served panel is substituted.** See "Method" above. The rest of the page,
  the portal session, the OAuth chain and the ai-server are real.
- **Case 5 stubs the `/oauth/launch` response** with a static 200. Without an AI
  session the real response chain ends at a login page whose breakout destroys
  the page under test, so the frame could not survive a collapse/re-open to be
  measured. The *request* is real and counted; only its body is substituted.
  Cases 2 and 4 leave the chain entirely real.
- **`pid 1`'s portal password was reset** by direct SQL/bcrypt before each run —
  the same category of deviation already recorded in
  `evidence/TICK-045/FIX_VERIFICATION.md` (the prior value was not known to this
  session either). Portal login for `pid 1` works after the pass.
- **Chrome runs with `--disable-features=LocalNetworkAccessChecks`.** Both
  `emr.localhost` and `chat.localhost` resolve to the loopback in this demo
  topology, so Chrome's Local Network Access check blocks the panel's
  cross-origin subresource before it is sent
  (`net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`), and the run would measure
  a launch that never reached the ai-server. A property of the all-on-loopback
  demo, not of the product. Worth its own ticket if the demo is ever expected to
  run in a stock browser profile.

## What this does NOT prove

- **Nothing about where an authorization lands.** Case 2 ends on
  `/oauth2/default/provider/login` and stops there. FR-31's destination rule is
  TICK-051's.
- **Nothing about a re-open with a live AI session.** `/oauth/launch` has no
  short-circuit yet, so re-opening the panel in a later page load still starts a
  fresh authorization. That is explicitly this ticket's Out of Scope and
  TICK-051's `/oauth/launch` criterion. AC3's "assigned once" is proven *within*
  a page load, which is the scope in which the transcript exists at all.
- **Nothing about the OAuth2 provider's separate session.** Whether OpenEMR can
  be made to accept the existing portal session instead of demanding a second
  sign-in is untouched upstream behaviour and still needs its own spike.
- **Only desktop Chrome, only this topology.** No other browser and no
  non-loopback deployment was exercised.
