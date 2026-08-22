// TICK-055 live verification: signing out of the OpenEMR portal must end the AI
// chat session, and the previous patient's session must not be resumable by
// navigating straight to the chat origin afterwards.
//
// Real desktop Chrome, real portal login as a seeded synthetic patient, real local
// Docker topology (Caddy + OpenEMR + ai-server + MariaDB). Unlike TICK-054's
// harness this patches nothing: `local-openemr-1` bind-mounts the module from THIS
// worktree and `local-ai-server-1` was rebuilt from it (`--build`; the ai-server is
// not bind-mounted), and the wrapper checks both against the host copies before the
// run. So the markup and the server behaviour measured here are the real ones.
//
// Every verdict has two independent witnesses:
//   * the browser -- a real click on the portal's own Logout control, a CDP network
//     capture of the beacon it fires, and a real HTTP status from a chat turn made
//     from the chat origin itself;
//   * the server -- the ai_session SQLite row set, read straight out of the
//     ai-server container before and after, so "the session ended" means the row
//     holding the patient's encrypted OpenEMR tokens is gone, not that a cookie
//     looked different.
//
// Run: node evidence/TICK-055/verify_portal_signout_ends_chat.mjs
// Exits non-zero if any assertion fails. Starts nothing that outlives it.

import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORTAL_LOGIN = 'https://emr.localhost/portal/index.php?site=default';
const DASHBOARD_MARKER = 'portal/home.php';
const CHAT_ORIGIN = 'https://chat.localhost';
const LOGOUT_URL = 'https://chat.localhost/api/logout';
const PORTAL_USER = process.env.TICK055_PORTAL_USER || 'AverySubjecttest1';
const PORTAL_PASS = process.env.TICK055_PORTAL_PASS || 'Tick055Verify!2026';

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
  const profile = mkdtempSync(join(tmpdir(), 'tick055-live-'));
  const debugPort = 9355;
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    '--ignore-certificate-errors',
    // Same all-on-loopback property TICK-054 recorded: emr.localhost and
    // chat.localhost both resolve to 127.0.0.1, so Chrome's Local Network Access
    // checks would block the panel's cross-origin subresource before it is sent.
    '--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks',
    '--no-first-run',
    '--window-size=1280,1000',
  ]);

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
        requests.push({
          url: msg.params.request.url,
          method: msg.params.request.method,
          key: `${msg.params.request.url}|${msg.params.timestamp}`,
          at: Date.now(),
        });
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
      console.log(`    screenshot: evidence/TICK-055/screenshot-${name}.png`);
    };
    const distinct = (rows) => {
      const seen = new Set();
      return rows.filter((r) => (seen.has(r.key) ? false : seen.add(r.key)));
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
     * Drive OpenEMR's OAuth2 patient login and scope consent to completion.
     *
     * These run at the TOP level, not inside the panel iframe: the vendor's
     * `oauth2-login.html.twig` carries a breakout script that hoists itself out of
     * any frame (TICK-045), so the tile click lands the whole window on the OAuth2
     * login page. Both forms submit through named submit buttons whose value is
     * part of the payload (`user_role=portal-api`, `proceed=1`), so they are
     * clicked rather than `requestSubmit()`ed -- a bare submit drops the value and
     * the login silently fails.
     */
    const completeOAuthSignIn = async () => {
      const seen = [];
      for (let step = 0; step < 8; step++) {
        const where = await evaluate('location.href');
        seen.push(where);
        if (where.startsWith(CHAT_ORIGIN)) return { done: true, seen };
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
      return { done: (await evaluate('location.href')).startsWith(CHAT_ORIGIN), seen };
    };

    /** A real chat turn, made from a page actually served by the chat origin. */
    const chatTurnFromChatOrigin = async () => {
      await navigate(`${CHAT_ORIGIN}/`, 2000);
      const href = await evaluate('location.href');
      if (!href.startsWith(CHAT_ORIGIN)) {
        throw new Error(`did not land on the chat origin: ${href}`);
      }
      return JSON.parse(
        await evaluate(`fetch("/api/chat", {
          method: "POST",
          credentials: "include",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({message: "Hello"})
        }).then(r => JSON.stringify({status: r.status}))
          .catch(e => JSON.stringify({status: -1, error: String(e)}))`),
      );
    };

    const before = sessionHandles();
    console.log(`\nai_session rows before the run: ${before.size}`);

    // ------------------------------------------------------------------ login
    console.log('\n== 1. portal login as the seeded synthetic patient ==');
    await navigate(PORTAL_LOGIN);
    await evaluate(`(() => {
      document.getElementById('uname').value = ${JSON.stringify(PORTAL_USER)};
      document.getElementById('pass').value = ${JSON.stringify(PORTAL_PASS)};
      document.querySelector('form[action="get_patient_info.php"]').requestSubmit();
    })()`);
    await sleep(5000);
    const afterLogin = await evaluate('location.href');
    check('portal login lands on the dashboard', afterLogin.includes(DASHBOARD_MARKER), afterLogin);
    if (!afterLogin.includes(DASHBOARD_MARKER)) {
      throw new Error(`login did not reach the dashboard: ${afterLogin}`);
    }
    await shot('1-dashboard');

    // ------------------------------------------------------- open the chat panel
    console.log('\n== 2. open the AI Chat panel and sign in (creates the AI session) ==');
    await clickElement('aeai-chat-go', 6000);
    const signIn = await completeOAuthSignIn();
    check(
      'the OAuth patient sign-in completes and lands on the chat origin',
      signIn.done,
      signIn.seen.at(-1),
    );
    const afterOpen = sessionHandles();
    const created = [...afterOpen].filter((h) => !before.has(h));
    check(
      'opening the panel created exactly one AI session',
      created.length === 1,
      `${created.length} new row(s); handle_hash=${created[0]?.slice(0, 16) ?? 'none'}...`,
    );
    if (created.length !== 1) {
      throw new Error('no single new AI session to verify against');
    }
    const patientHandle = created[0];
    await shot('2-chat-panel-open');

    // ------------------------------------- prove the session is live and usable
    console.log('\n== 3. hold a chat turn from the chat origin (session is live) ==');
    const liveTurn = await chatTurnFromChatOrigin();
    check(
      'a chat turn on the live session is accepted (not 401)',
      liveTurn.status === 200,
      `HTTP ${liveTurn.status}${liveTurn.error ? ` ${liveTurn.error}` : ''}`,
    );
    await shot('3-chat-origin-while-signed-in');

    // ------------------------------------------------------ sign out of the portal
    console.log('\n== 4. sign out of the OpenEMR portal (real click on its Logout) ==');
    await navigate(`https://emr.localhost/portal/${DASHBOARD_MARKER.split('/')[1]}`, 4000);
    const backOnDashboard = await evaluate('location.href');
    if (!backOnDashboard.includes(DASHBOARD_MARKER)) {
      throw new Error(`could not return to the dashboard to sign out: ${backOnDashboard}`);
    }
    const logoutHref = await evaluate(
      "(document.querySelector(\"a[href$='logout.php']\") || {}).getAttribute" +
        " ? document.querySelector(\"a[href$='logout.php']\").getAttribute('href') : 'none'",
    );
    check('the portal renders a sign-out link the hook can bind', logoutHref !== 'none', logoutHref);
    const beaconsBefore = distinct(requests.filter((r) => r.url.startsWith(LOGOUT_URL))).length;
    await clickElement('logout-go', 6000);
    const beacons = distinct(requests.filter((r) => r.url.startsWith(LOGOUT_URL)));
    check(
      'the sign-out click fires a POST to the AI server logout endpoint',
      beacons.length === beaconsBefore + 1 && beacons.at(-1)?.method === 'POST',
      `${beacons.length - beaconsBefore} request(s), method=${beacons.at(-1)?.method}`,
    );
    const afterLogoutUrl = await evaluate('location.href');
    check(
      'the portal itself signed out (left the dashboard)',
      !afterLogoutUrl.includes(DASHBOARD_MARKER),
      afterLogoutUrl,
    );
    await shot('4-after-portal-signout');

    // ------------------------------------------------- the server-side witness
    console.log('\n== 5. the AI session row is gone from the ai-server ==');
    const afterLogout = sessionHandles();
    check(
      "the patient's AI session row is deleted, not merely expired",
      !afterLogout.has(patientHandle),
      `handle_hash=${patientHandle.slice(0, 16)}... present=${afterLogout.has(patientHandle)}`,
    );
    check(
      'no other session row was collaterally deleted',
      [...before].every((h) => afterLogout.has(h)),
      `${afterLogout.size} row(s) remain, was ${before.size} before the run`,
    );

    // ----------------------------- navigate straight to the chat origin (AC3)
    console.log('\n== 6. navigate directly to the chat origin after signing out ==');
    const deadTurn = await chatTurnFromChatOrigin();
    check(
      'the previous patient’s session does not resume: /api/chat is 401',
      deadTurn.status === 401,
      `HTTP ${deadTurn.status}${deadTurn.error ? ` ${deadTurn.error}` : ''}`,
    );
    const pageServed = await evaluate('document.title');
    check(
      'GET / still serves only the empty chat shell, with no transcript',
      (await evaluate("document.getElementById('chat-transcript').children.length")) === 0,
      `title=${pageServed}`,
    );
    await shot('5-chat-origin-after-signout');
  } finally {
    chrome.kill('SIGTERM');
    await sleep(500);
    chrome.kill('SIGKILL');
    try {
      require('node:fs').rmSync(profile, { recursive: true, force: true });
    } catch {
      /* best effort */
    }
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    for (const f of failed) console.log(`  FAILED: ${f.name}`);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(`\nHARNESS ERROR: ${err.stack || err.message}`);
  process.exit(2);
});
