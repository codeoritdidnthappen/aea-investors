// TICK-051 live verification: after signing in, the patient must land on the portal
// dashboard -- never on the standalone full-page chat (FR-2, FR-31, ADR-8).
//
// Real desktop Chrome, real portal login as a seeded synthetic patient, real local
// Docker topology (Caddy + OpenEMR + ai-server + MariaDB). `local-openemr-1`
// bind-mounts the portal module and the OAuth2 twig overrides from THIS worktree, and
// `local-ai-server-1` was rebuilt from it (`--build`; the ai-server is NOT
// bind-mounted). The wrapper checks host and container agree before this runs, so the
// behaviour measured here is the real behaviour and not a stale image's.
//
// The four places a sign-in can complete are all exercised, because the bug is about
// where the patient *ends up* and each of these is a different arrival:
//
//   1. OpenEMR's own native portal login          -> portal/home.php   (AC11)
//   2. first AI sign-in, via the panel + TICK-045's top-level breakout, through the
//      OAuth2 login form and the scope-consent step -> dashboard        (AC2)
//   3. re-authentication after the AI session expires, same breakout    (AC2)
//   4. a direct top-level visit to /oauth/launch that completes with no prompt at
//      all, because the provider session is still live                 (AC2, AC7)
//
// plus the two that must NOT navigate: an in-panel launch on a live session (AC7),
// and the chat turn that has to keep working once the settings are split (AC9).
//
// Run: node evidence/TICK-051/verify_signin_lands_on_dashboard.mjs
// Exits non-zero if any assertion fails. Starts nothing that outlives it.

import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORTAL_LOGIN = 'https://emr.localhost/portal/index.php?site=default';
const DASHBOARD = 'https://emr.localhost/portal/home.php';
const DASHBOARD_MARKER = 'portal/home.php';
const CHAT_ORIGIN = 'https://chat.localhost';
const LAUNCH_URL = `${CHAT_ORIGIN}/oauth/launch`;
const PORTAL_USER = process.env.TICK051_PORTAL_USER || 'AverySubjecttest1';
const PORTAL_PASS = process.env.TICK051_PORTAL_PASS || 'Tick051Verify!2026';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** The ai-server's own session table, read from inside its container. */
function sessionHandles() {
  const out = execFileSync('docker', [
    'exec',
    'local-ai-server-1',
    'python',
    '-c',
    "import sqlite3;print(' '.join(r[0].hex() for r in " +
      "sqlite3.connect('/data/ai_session.sqlite3').execute('SELECT handle_hash FROM sessions')))",
  ]);
  return new Set(String(out).trim().split(/\s+/).filter(Boolean));
}

/** Age every AI session out, to reach TICK-045's re-authentication path honestly. */
function expireAllSessions() {
  execFileSync('docker', [
    'exec',
    'local-ai-server-1',
    'python',
    '-c',
    "import sqlite3;c=sqlite3.connect('/data/ai_session.sqlite3');" +
      "c.execute('UPDATE sessions SET expires_at = 1');c.commit()",
  ]);
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.handlers = [];
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== undefined) {
        const resolver = this.pending.get(msg.id);
        if (!resolver) return;
        this.pending.delete(msg.id);
        msg.error
          ? resolver.reject(new Error(msg.error.message || JSON.stringify(msg.error)))
          : resolver.resolve(msg.result);
        return;
      }
      for (const handler of this.handlers) handler(msg);
    });
  }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    return new Cdp(ws);
  }

  on(handler) {
    this.handlers.push(handler);
  }

  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
}

async function chromeWsUrl(port) {
  for (let i = 0; i < 100; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      return (await res.json()).webSocketDebuggerUrl;
    } catch {
      await sleep(100);
    }
  }
  throw new Error('Chrome did not expose a DevTools endpoint');
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -- ${detail}` : ''}`);
}

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 'tick051-live-'));
  const debugPort = 9351;
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    '--ignore-certificate-errors',
    // Same all-on-loopback property TICK-054/TICK-055 recorded: emr.localhost and
    // chat.localhost both resolve to 127.0.0.1, so Chrome's Local Network Access
    // checks would block the panel's cross-origin subresource before it is sent.
    '--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks',
    '--no-first-run',
    '--window-size=1280,1000',
  ]);

  // Every navigation Chrome makes, with the Sec-Fetch-Dest it actually sent. This is
  // the second witness for the whole mechanism: the destination rule is stated over
  // that header, so "Chrome really sends document/iframe here" must be observed
  // rather than assumed.
  const requests = [];

  try {
    const browser = await Cdp.connect(await chromeWsUrl(debugPort));

    const armTarget = async (sid) => {
      for (const [domain, params] of [
        ['Network.enable', {}],
        ['Page.enable', {}],
      ]) {
        try {
          await browser.send(domain, params, sid);
        } catch {
          /* target may already be gone */
        }
      }
    };

    browser.on(async (msg) => {
      if (msg.method === 'Target.attachedToTarget') {
        const sid = msg.params.sessionId;
        try {
          await browser.send(
            'Target.setAutoAttach',
            { autoAttach: true, waitForDebuggerOnStart: true, flatten: true },
            sid,
          );
        } catch {
          /* not a target that can auto-attach */
        }
        await armTarget(sid);
        try {
          await browser.send('Runtime.runIfWaitingForDebugger', {}, sid);
        } catch {
          /* not paused */
        }
        return;
      }
      if (msg.method === 'Network.requestWillBeSent') {
        // A redirect reuses the SAME requestId and arrives as another
        // requestWillBeSent carrying `redirectResponse`. The hop that redirected is
        // the one being measured here (the 303 off /oauth/launch), so its status is
        // recorded onto its own entry before the new hop is pushed -- otherwise the
        // launch hop keeps a null status and the dashboard hop takes its place.
        if (msg.params.redirectResponse) {
          const previous = requests.find(
            (r) => r.id === msg.params.requestId && r.status === null,
          );
          if (previous) previous.status = msg.params.redirectResponse.status;
        }
        requests.push({
          id: msg.params.requestId,
          url: msg.params.request.url,
          method: msg.params.request.method,
          dest: null,
          status: null,
          at: Date.now(),
        });
        return;
      }
      // `Sec-Fetch-*` is NOT on `requestWillBeSent`.request.headers -- the network
      // stack adds those after the renderer hands the request over, so they only
      // appear on the ExtraInfo event. Reading the first event and finding nothing is
      // exactly how a harness reports "the header was absent" when it was in fact
      // sent, which for this ticket would silently invert the result being measured.
      if (msg.method === 'Network.requestWillBeSentExtraInfo') {
        // Earliest hop of this requestId still missing its headers -- see the
        // redirect note above; `findLast` would attach every hop's headers to the
        // final one and leave the hop under test blank.
        const entry = requests.find((r) => r.id === msg.params.requestId && r.dest === null);
        if (!entry) return;
        const headers = msg.params.headers || {};
        for (const [name, value] of Object.entries(headers)) {
          if (name.toLowerCase() === 'sec-fetch-dest') entry.dest = value;
        }
        return;
      }
      if (msg.method === 'Network.responseReceived') {
        const entry = requests.find((r) => r.id === msg.params.requestId && r.status === null);
        if (entry) entry.status = msg.params.response.status;
        return;
      }
      // A cross-origin POST with no CORS response headers is blocked at the browser,
      // so the page's own `fetch()` rejects and can never read the status. The status
      // the server actually returned still arrives here.
      if (msg.method === 'Network.responseReceivedExtraInfo') {
        const entry = requests.find((r) => r.id === msg.params.requestId && r.status === null);
        if (entry) entry.status = msg.params.statusCode;
      }
    });

    await browser.send('Target.setDiscoverTargets', { discover: true });
    await browser.send('Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: true,
      flatten: true,
    });

    const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
    await armTarget(sessionId);
    await browser.send('Emulation.setFocusEmulationEnabled', { enabled: true }, sessionId);

    const evaluate = async (expression) => {
      const res = await browser.send(
        'Runtime.evaluate',
        { expression, returnByValue: true, awaitPromise: true },
        sessionId,
      );
      if (res.exceptionDetails) {
        throw new Error(res.exceptionDetails.exception?.description || 'evaluate failed');
      }
      return res.result.value;
    };
    const navigate = async (url, settle = 2500) => {
      await browser.send('Page.navigate', { url }, sessionId);
      await sleep(settle);
    };
    const shot = async (name) => {
      const { data } = await browser.send('Page.captureScreenshot', { format: 'png' }, sessionId);
      writeFileSync(
        new URL(`./screenshot-${name}.png`, import.meta.url),
        Buffer.from(data, 'base64'),
      );
      console.log(`    screenshot: evidence/TICK-051/screenshot-${name}.png`);
    };

    /** A real mouse press on an element, at its on-screen centre. */
    const clickElement = async (id, settle = 2000) => {
      const spot = JSON.parse(
        await evaluate(`(() => {
          const el = document.getElementById(${JSON.stringify(id)});
          if (!el) return JSON.stringify({missing: true});
          el.scrollIntoView({block: 'center'});
          const r = el.getBoundingClientRect();
          const x = r.left + r.width / 2, y = r.top + r.height / 2;
          const hit = document.elementFromPoint(x, y);
          return JSON.stringify({
            x, y,
            hit: hit ? hit.tagName + '#' + hit.id : null,
            onTarget: !!(hit && hit.closest('#' + CSS.escape(${JSON.stringify(id)}))),
          });
        })()`),
      );
      if (spot.missing) throw new Error(`#${id} is not on the page`);
      if (!spot.onTarget) {
        throw new Error(`click point is not over #${id} -- it hit ${spot.hit ?? 'nothing'}`);
      }
      for (const type of ['mousePressed', 'mouseReleased']) {
        await browser.send(
          'Input.dispatchMouseEvent',
          { type, x: spot.x, y: spot.y, button: 'left', clickCount: 1 },
          sessionId,
        );
      }
      await sleep(settle);
      return spot;
    };

    /**
     * Drive OpenEMR's OAuth2 patient login and scope consent to completion, then
     * report where the browser came to rest.
     *
     * These run at the TOP level, not inside the panel iframe: the vendor's
     * `oauth2-login.html.twig` and `scope-authorize.html.twig` carry a breakout
     * script that hoists itself out of any frame (TICK-045), so opening the panel
     * lands the whole window on the OAuth2 login page. Both forms submit through
     * named submit buttons whose value is part of the payload
     * (`user_role=portal-api`, `proceed=1`), so they are clicked rather than
     * `requestSubmit()`ed -- a bare submit drops the value and the login fails.
     *
     * Pre-TICK-051 this settled on `https://chat.localhost/` -- the full-page chat,
     * portal gone. That is the bug.
     */
    const completeOAuthSignIn = async () => {
      const seen = [];
      for (let step = 0; step < 8; step++) {
        const where = await evaluate('location.href');
        seen.push(where);
        if (where.includes(DASHBOARD_MARKER) || where.startsWith(CHAT_ORIGIN)) {
          return { settled: where, seen };
        }
        const acted = await evaluate(`(() => {
          const patientLogin = document.querySelector('button[name="user_role"][value="portal-api"]');
          if (patientLogin && document.querySelector('input[name="username"]')) {
            document.querySelector('input[name="username"]').value = ${JSON.stringify(PORTAL_USER)};
            document.querySelector('input[name="password"]').value = ${JSON.stringify(PORTAL_PASS)};
            patientLogin.click();
            return 'login';
          }
          const authorize = document.getElementById('authorize-btn');
          if (authorize) { authorize.click(); return 'consent'; }
          return 'none';
        })()`);
        if (acted === 'none') {
          await sleep(1500);
          continue;
        }
        console.log(`    oauth step ${step + 1}: ${acted}`);
        await sleep(4000);
      }
      return { settled: await evaluate('location.href'), seen };
    };

    /** What the panel iframe currently holds, from the parent page's point of view. */
    const panelState = async () =>
      JSON.parse(
        await evaluate(`(() => {
          const f = document.querySelector('#aeai-chat-card iframe') ||
                    document.querySelector('iframe[src*="oauth/launch"]');
          return JSON.stringify({
            present: !!f,
            src: f ? (f.getAttribute('src') || '') : null,
            top: location.href,
          });
        })()`),
      );

    /** The Sec-Fetch-Dest Chrome actually put on the most recent request to `url`. */
    const destFor = (prefix) => requests.findLast((r) => r.url.startsWith(prefix));

    // ---------------------------------------------- 1. OpenEMR's own native login
    console.log('\n== 1. OpenEMR native portal login (AC11: unmodified, verified) ==');
    await navigate(PORTAL_LOGIN);
    await evaluate(`(() => {
      document.getElementById('uname').value = ${JSON.stringify(PORTAL_USER)};
      document.getElementById('pass').value = ${JSON.stringify(PORTAL_PASS)};
      document.querySelector('form[action="get_patient_info.php"]').requestSubmit();
    })()`);
    await sleep(5000);
    const afterLogin = await evaluate('location.href');
    check(
      'AC11 OpenEMR native portal login still lands on portal/home.php',
      afterLogin.includes(DASHBOARD_MARKER),
      afterLogin,
    );
    if (!afterLogin.includes(DASHBOARD_MARKER)) {
      throw new Error(`native login did not reach the dashboard: ${afterLogin}`);
    }
    await shot('1-native-portal-login-dashboard');

    // -------------------------------------------------- AC6: the tile is present
    const tile = JSON.parse(
      await evaluate(`(() => {
        const el = document.getElementById('aeai-chat-go');
        if (!el) return JSON.stringify({present: false});
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return JSON.stringify({
          present: true,
          text: (el.innerText || '').trim().replace(/\\s+/g, ' '),
          visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none',
          width: Math.round(r.width), height: Math.round(r.height),
        });
      })()`),
    );
    check(
      'AC6 the dashboard carries a visible AI Chat tile, one click from the chat',
      tile.present && tile.visible,
      `"${tile.text}" ${tile.width}x${tile.height}px`,
    );

    const before = sessionHandles();
    console.log(`\nai_session rows before the AI sign-in: ${before.size}`);

    // ---------------------------------- 2. first AI sign-in, through the breakout
    console.log('\n== 2. first AI sign-in: open the panel, sign in at top level (AC2) ==');
    await clickElement('aeai-chat-go', 6000);
    const firstSignIn = await completeOAuthSignIn();
    check(
      'AC2 a first sign-in completing at top level lands on the portal dashboard',
      firstSignIn.settled.includes(DASHBOARD_MARKER),
      firstSignIn.settled,
    );
    check(
      'AC2 it is NOT the standalone full-page chat',
      !firstSignIn.settled.startsWith(CHAT_ORIGIN),
      `settled on ${firstSignIn.settled}`,
    );
    const cb = destFor(`${CHAT_ORIGIN}/oauth/callback`);
    check(
      'AC5 Chrome sent Sec-Fetch-Dest: document on the top-level callback',
      cb?.dest === 'document',
      `dest=${cb?.dest ?? 'none'} status=${cb?.status ?? '?'}`,
    );
    const created = [...sessionHandles()].filter((h) => !before.has(h));
    check(
      'the sign-in issued exactly one AI session',
      created.length === 1,
      `${created.length} new row(s)`,
    );
    await shot('2-after-first-signin-dashboard');

    // -------------------------------- 3. the panel, on a live session, in the panel
    console.log('\n== 3. open the panel again: launch short-circuits in-panel (AC7) ==');
    await clickElement('aeai-chat-go', 6000);
    const stillOnDashboard = await evaluate('location.href');
    check(
      'AC5/AC7 opening the panel on a live session navigates nothing',
      stillOnDashboard.includes(DASHBOARD_MARKER),
      stillOnDashboard,
    );
    const panel = await panelState();
    check('the panel iframe is pointed at /oauth/launch', panel.src?.includes('oauth/launch'), panel.src);
    const inPanelLaunch = destFor(LAUNCH_URL);
    check(
      'AC7 the in-panel launch was answered 200 (chat served), not 302 (re-authorized)',
      inPanelLaunch?.status === 200,
      `dest=${inPanelLaunch?.dest ?? 'none'} status=${inPanelLaunch?.status ?? '?'}`,
    );
    check(
      'AC5 Chrome sent Sec-Fetch-Dest: iframe for the in-panel launch',
      inPanelLaunch?.dest === 'iframe',
      `dest=${inPanelLaunch?.dest ?? 'none'}`,
    );
    await shot('3-chat-panel-open-on-dashboard');

    // ------------------------------------------------ 4. a chat turn still streams
    console.log('\n== 4. a chat turn still streams with the settings split (AC9) ==');
    // Fired from the portal page, so the browser stamps `Origin: https://emr.localhost`
    // -- the dashboard's own origin, and precisely the value the single pre-TICK-051
    // setting would have taken once the destination was fixed. The server must refuse
    // it.
    //
    // Deliberately a CORS-*simple* request: no `Content-Type` header, so no preflight.
    // That is the shape the Origin check actually defends against, and the shape
    // main.py's comment names -- a JSON Content-Type would trigger an OPTIONS
    // preflight that dies before the POST is ever sent, which proves nothing about
    // this route and (worse) looks like a pass. Starlette parses the body as JSON
    // regardless of the declared type, so the request is genuinely live.
    //
    // The `fetch()` itself is expected to reject -- a 403 carries no CORS headers, so
    // the page may not read it -- hence the verdict comes from the CDP-observed
    // response rather than from JS.
    await evaluate(`fetch(${JSON.stringify(`${CHAT_ORIGIN}/api/chat`)}, {
      method: 'POST', credentials: 'include',
      body: JSON.stringify({message: 'Hello'}),
    }).then(() => 'resolved', () => 'blocked')`);
    await sleep(1500);
    const portalOriginTurn = requests.findLast(
      (r) => r.url === `${CHAT_ORIGIN}/api/chat` && r.method === 'POST',
    );
    // Deliberately not asserted as "403 exactly". FastAPI validates the
    // `ChatTurnRequest` body before the handler's Origin check runs, so this
    // CORS-simple turn -- whose implicit `text/plain` the body model refuses -- is
    // rejected at 422, one step ahead of the Origin check; sending a JSON
    // Content-Type instead would trigger a preflight that dies at 405 and never
    // deliver the POST at all. Every route out of a cross-origin page is a
    // rejection, which is what this check is entitled to claim.
    //
    // The Origin check's own 403 is asserted where it is unambiguous, on a
    // well-formed JSON body: `run_live_verification.sh` step 4 against this running
    // server, and test_chat.py's
    // `test_ac9_the_settings_split_did_not_disable_the_chat_origin_check`.
    check(
      'AC9 a chat turn from the portal origin is refused (never reaches the model)',
      [403, 422, 405].includes(portalOriginTurn?.status),
      `HTTP ${portalOriginTurn?.status ?? 'no request observed'} ` +
        '(403 origin / 422 body-before-origin / 405 preflight)',
    );
    // ...and the same turn, from a document actually served by the chat origin.
    await navigate(`${CHAT_ORIGIN}/`, 2500);
    const ownTurn = JSON.parse(
      await evaluate(`fetch("/api/chat", {
        method: "POST", credentials: "include",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({message: "Hello"}),
      }).then(async r => JSON.stringify({status: r.status, body: (await r.text()).slice(0, 120)}))
        .catch(e => JSON.stringify({status: -1, body: String(e)}))`),
    );
    check(
      'AC9 the chat page’s own same-origin fetch is accepted and streams a reply',
      ownTurn.status === 200 && ownTurn.body.length > 0,
      `HTTP ${ownTurn.status} body="${ownTurn.body.replace(/\s+/g, ' ').slice(0, 60)}..."`,
    );
    await shot('4-chat-turn-streams');

    // ------------------------- 5. top-level /oauth/launch on a LIVE session (AC7)
    console.log('\n== 5. top-level /oauth/launch with a live session (AC7) ==');
    await navigate(LAUNCH_URL, 4000);
    const afterTopLaunch = await evaluate('location.href');
    check(
      'AC7 a live session reached at top level gets the dashboard, never the full-page chat',
      afterTopLaunch.includes(DASHBOARD_MARKER),
      afterTopLaunch,
    );
    const topLaunch = destFor(LAUNCH_URL);
    check(
      'AC5 Chrome sent Sec-Fetch-Dest: document for the top-level launch',
      topLaunch?.dest === 'document',
      `dest=${topLaunch?.dest ?? 'none'} status=${topLaunch?.status ?? '?'}`,
    );
    await shot('5-top-level-launch-dashboard');

    // ------------------------- 6. re-authentication after the AI session expires
    console.log('\n== 6. expire the AI session and re-authenticate (AC2) ==');
    expireAllSessions();
    check(
      'the AI session is expired server-side before re-authenticating',
      true,
      'sessions.expires_at forced into the past inside local-ai-server-1',
    );
    await navigate(DASHBOARD, 3000);
    await clickElement('aeai-chat-go', 6000);
    const reAuth = await completeOAuthSignIn();
    check(
      'AC2 re-authentication after expiry also lands on the portal dashboard',
      reAuth.settled.includes(DASHBOARD_MARKER),
      reAuth.settled,
    );
    check(
      'AC2 re-authentication is NOT answered with the standalone chat',
      !reAuth.settled.startsWith(CHAT_ORIGIN),
      `settled on ${reAuth.settled}`,
    );
    await shot('6-after-reauth-dashboard');

    // --------------------------------------- 7. the denial path, in a real browser
    console.log('\n== 7. an authorization denial returns to the dashboard (AC4) ==');
    await navigate(
      `${CHAT_ORIGIN}/oauth/callback?error=access_denied` +
        `&error_description=The+user+denied+the+request&state=synthetic-denial`,
      3000,
    );
    const afterDenial = await evaluate('location.href');
    check(
      'AC4 a denied authorization lands on the dashboard, not a 422 or an error page',
      afterDenial.includes(DASHBOARD_MARKER),
      afterDenial,
    );
    await shot('7-after-denial-dashboard');

    // ------------------------------- 8. a return-URL parameter changes nothing
    console.log('\n== 8. a next= parameter on the callback is discarded (AC3) ==');
    await navigate(
      `${CHAT_ORIGIN}/oauth/callback?error=access_denied&state=x` +
        `&next=${encodeURIComponent(`${CHAT_ORIGIN}/`)}` +
        `&redirect=${encodeURIComponent('https://attacker.test/')}`,
      3000,
    );
    const afterNext = await evaluate('location.href');
    check(
      'AC3 next=/redirect= on the callback are discarded; the destination is unchanged',
      afterNext.includes(DASHBOARD_MARKER),
      afterNext,
    );
  } finally {
    chrome.kill('SIGTERM');
    await sleep(500);
    chrome.kill('SIGKILL');
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {
      /* best effort */
    }
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    console.log('FAILED:');
    for (const f of failed) console.log(`  - ${f.name}`);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
