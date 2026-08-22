// TICK-054 live verification: the AI Chat panel must start no authorization
// until the patient opens it.
//
// Real desktop Chrome, real portal login as a seeded synthetic patient, real
// local Docker topology (Caddy + OpenEMR + ai-server + MariaDB). Every verdict
// below is decided by a *network capture* -- CDP `Network.requestWillBeSent`
// across the main target and every out-of-process `chat.localhost` frame --
// not by reading markup. The ai-server's own request log is diffed
// independently by the shell wrapper as a second, server-side witness.
//
// The running `local-openemr-1` bind-mounts the module from the MAIN repo
// checkout, not from this build worktree, so it still serves the pre-TICK-054
// panel (`src="https://chat.localhost/oauth/launch"`). Rather than mutate the
// container or the main checkout -- the mount hazard recorded in
// evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md -- this intercepts the real
// dashboard response and swaps the old panel block for `rendered_panel.html`,
// which is this worktree's PortalChatController executed by the container's own
// PHP (see render_panel_probe.php). Byte for byte the HTML the container will
// serve once this ticket merges.
//
// Run: node evidence/TICK-054/verify_deferred_chat_launch.mjs
// Exits non-zero if any assertion fails. Starts nothing that outlives it.

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME =
  '/Users/megalodon/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/' +
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const PORTAL_LOGIN = 'https://emr.localhost/portal/index.php?site=default';
const DASHBOARD_MARKER = 'portal/home.php';
const LAUNCH_URL = 'https://chat.localhost/oauth/launch';
const CHAT_ORIGIN = 'chat.localhost';
const PORTAL_USER = process.env.TICK054_PORTAL_USER || 'AverySubjecttest1';
const PORTAL_PASS = process.env.TICK054_PORTAL_PASS || 'Tick054Verify!2026';
const PANEL_HTML = readFileSync(new URL('./rendered_panel.html', import.meta.url), 'utf8').trim();
const OLD_PANEL_OPEN = '<div id="aeai-portal-chat"';
const OLD_PANEL_CLOSE = '</iframe></div></div>';
const STUB_BODY =
  '<!doctype html><meta charset="utf-8"><title>AEAI launch stub</title>' +
  '<body style="font:16px sans-serif;padding:1rem">stub chat surface</body>';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

/** Replace the served (pre-TICK-054) panel block with this worktree's render. */
function patchDashboard(html) {
  const start = html.indexOf(OLD_PANEL_OPEN);
  if (start === -1) return null;
  const end = html.indexOf(OLD_PANEL_CLOSE, start);
  if (end === -1) return null;
  return html.slice(0, start) + PANEL_HTML + html.slice(end + OLD_PANEL_CLOSE.length);
}

async function main() {
  if (!PANEL_HTML.includes('data-src="')) {
    throw new Error('rendered_panel.html has no data-src -- regenerate it from this worktree');
  }
  const profile = mkdtempSync(join(tmpdir(), 'tick054-live-'));
  const debugPort = 9354;
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    '--ignore-certificate-errors',
    // emr.localhost and chat.localhost both resolve to the loopback, so Chrome's Local
    // Network Access checks block the panel's cross-origin subresource before it is
    // ever sent -- the iframe dies with ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS and
    // the run measures a launch that never reached the ai-server. A property of this
    // all-on-loopback demo topology, not of the product.
    '--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks',
    '--no-first-run',
    '--window-size=1280,1000',
  ]);

  // The network capture. Every request Chrome makes, from any target.
  const requests = [];
  const failures = [];
  let stubLaunch = false;
  let dashboardsPatched = 0;

  try {
    const browser = await Cdp.connect(await chromeWsUrl(debugPort));

    // Intercept only the two URLs this harness has to rewrite. An `urlPattern: '*'`
    // response-stage interception also pauses the launch's own 302 and the OAuth2
    // redirect chain behind it, which cannot be resumed with `Fetch.continueRequest`
    // -- the iframe then dies with a network error and the run silently measures
    // nothing. Everything else reaches the real Caddy/OpenEMR/ai-server untouched.
    const FETCH_PATTERNS = [
      { urlPattern: `*${DASHBOARD_MARKER}*`, requestStage: 'Response' },
      { urlPattern: `${LAUNCH_URL}*`, requestStage: 'Request' },
    ];

    const armTarget = async (sid) => {
      for (const [domain, params] of [
        ['Network.enable', {}],
        ['Page.enable', {}],
        ['Fetch.enable', { patterns: FETCH_PATTERNS }],
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
        // One request is reported once per attached session that can see it, so an
        // out-of-process `chat.localhost` frame is announced by both the parent target
        // and its own. CDP's monotonic `timestamp` is identical across those reports,
        // so (url, timestamp) is the request identity here -- counting raw events would
        // double every cross-origin frame load.
        requests.push({
          url: msg.params.request.url,
          key: `${msg.params.request.url}|${msg.params.timestamp}`,
          at: Date.now(),
        });
        return;
      }

      if (msg.method === 'Network.loadingFailed' && msg.params.errorText !== 'net::ERR_ABORTED') {
        failures.push(msg.params.errorText);
        return;
      }

      if (msg.method === 'Fetch.requestPaused') {
        const { requestId, request, responseStatusCode, responseHeaders } = msg.params;
        const sid = msg.sessionId;
        const passThrough = async () => {
          try {
            await browser.send('Fetch.continueRequest', { requestId }, sid);
          } catch {
            /* raced with a navigation */
          }
        };

        if (request.url.startsWith(LAUNCH_URL)) {
          if (!stubLaunch) {
            await passThrough();
            return;
          }
          try {
            await browser.send(
              'Fetch.fulfillRequest',
              {
                requestId,
                responseCode: 200,
                responseHeaders: [{ name: 'content-type', value: 'text/html; charset=utf-8' }],
                body: Buffer.from(STUB_BODY, 'utf8').toString('base64'),
              },
              sid,
            );
          } catch {
            await passThrough();
          }
          return;
        }

        if (!request.url.includes(DASHBOARD_MARKER) || responseStatusCode !== 200) {
          await passThrough();
          return;
        }

        try {
          const { body, base64Encoded } = await browser.send(
            'Fetch.getResponseBody',
            { requestId },
            sid,
          );
          const html = base64Encoded ? Buffer.from(body, 'base64').toString('utf8') : body;
          const swapped = patchDashboard(html);
          if (swapped === null) {
            console.log(`    (dashboard had no aeai panel to patch: ${html.length} bytes)`);
            await passThrough();
            return;
          }
          dashboardsPatched += 1;
          await browser.send(
            'Fetch.fulfillRequest',
            {
              requestId,
              responseCode: 200,
              responseHeaders: (responseHeaders || []).filter(
                (h) => !['content-length', 'content-encoding'].includes(h.name.toLowerCase()),
              ),
              body: Buffer.from(swapped, 'utf8').toString('base64'),
            },
            sid,
          );
        } catch (err) {
          console.log(`    !! interception error: ${err.message}`);
          await passThrough();
        }
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
    // Headless Chrome's page is not the OS focus owner, so `element.focus()` is a no-op
    // without this -- which would make the keyboard case silently untestable.
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
    const navigate = async (url) => {
      await browser.send('Page.navigate', { url }, sessionId);
      await sleep(2500);
    };
    const shot = async (name) => {
      const { data } = await browser.send('Page.captureScreenshot', { format: 'png' }, sessionId);
      writeFileSync(new URL(`./screenshot-${name}.png`, import.meta.url), Buffer.from(data, 'base64'));
      console.log(`    screenshot: evidence/TICK-054/screenshot-${name}.png`);
    };
    const distinct = (rows) => {
      const seen = new Set();
      return rows.filter((r) => (seen.has(r.key) ? false : seen.add(r.key)));
    };
    const chatRequests = (since = 0) =>
      distinct(requests.filter((r) => r.url.includes(CHAT_ORIGIN) && r.at >= since));
    const launchRequests = (since = 0) =>
      distinct(requests.filter((r) => r.url.startsWith(LAUNCH_URL) && r.at >= since));
    const authorizeRequests = (since = 0) =>
      distinct(
        requests.filter((r) => r.url.includes('/oauth2/default/') && r.at >= since),
      );

    /** A real mouse press on an element, at its on-screen centre. */
    const clickElement = async (id, settle = 1800) => {
      const box = await evaluate(`(() => {
        const tile = document.getElementById(${JSON.stringify(id)});
        if (!tile) return JSON.stringify({missing: true});
        tile.scrollIntoView({block: 'center'});
        const r = tile.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const hit = document.elementFromPoint(x, y);
        return JSON.stringify({
          x: x, y: y,
          hit: hit ? hit.tagName + '#' + hit.id + '.' + hit.className.split(' ')[0] : null,
          onTarget: !!(hit && hit.closest('#' + CSS.escape(${JSON.stringify(id)}))),
        });
      })()`);
      const spot = JSON.parse(box);
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
    const clickTile = (settle) => clickElement('aeai-chat-go', settle);

    /** Keyboard activation: focus the tile anchor, press Enter (NFR-19). */
    const enterOnTile = async () => {
      await evaluate("document.getElementById('aeai-chat-go').focus()");
      const focused = await evaluate('document.activeElement && document.activeElement.id');
      for (const type of ['keyDown', 'char', 'keyUp']) {
        await browser.send(
          'Input.dispatchKeyEvent',
          {
            type,
            key: 'Enter',
            code: 'Enter',
            windowsVirtualKeyCode: 13,
            nativeVirtualKeyCode: 13,
            text: '\r',
            unmodifiedText: '\r',
          },
          sessionId,
        );
      }
      await sleep(1800);
      return focused;
    };

    const panelState = async () =>
      JSON.parse(
        await evaluate(`(() => {
          const panel = document.getElementById('aeai-portal-chat');
          const frame = panel && panel.querySelector('iframe[data-aeai-portal-chat]');
          return JSON.stringify({
            panelPresent: !!panel,
            shown: !!panel && panel.classList.contains('show'),
            classes: panel ? panel.className : null,
            parent: panel && panel.getAttribute('data-parent'),
            tilePresent: !!document.getElementById('aeai-chat-go'),
            tileVisible: !!(document.getElementById('aeai-chat-go') || {}).offsetParent,
            dashboardShown: !!(document.getElementById('quickstart-card') || {classList: {contains: () => false}})
              .classList.contains('show'),
            tileToggle: (document.getElementById('aeai-chat-go') || {}).getAttribute
              ? document.getElementById('aeai-chat-go').getAttribute('data-toggle') : null,
            tileParent: document.getElementById('aeai-chat-go')
              ? document.getElementById('aeai-chat-go').getAttribute('data-parent') : null,
            src: frame ? frame.getAttribute('src') : null,
            dataSrc: frame ? frame.getAttribute('data-src') : null,
            whereto: typeof whereto === 'undefined' ? null : whereto,
          });
        })()`),
      );

    // ---------------------------------------------------------------- login
    console.log('\n== portal login as the seeded synthetic patient ==');
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
    check(
      'dashboard response was patched to this worktree panel',
      dashboardsPatched >= 1,
      `${dashboardsPatched} patched`,
    );

    // ------------------------------------------- case 1: dashboard render
    console.log('\n== case 1: dashboard render (no AI session present) ==');
    const state1 = await panelState();
    const chat1 = chatRequests();
    check(
      'AC1 dashboard render issued no request to the AI server',
      chat1.length === 0,
      `${chat1.length} ${CHAT_ORIGIN} requests${chat1.length ? `: ${chat1.map((r) => r.url).join(', ')}` : ''}`,
    );
    check('panel renders with no src attribute', state1.src === null, `src=${state1.src}`);
    check(
      'panel carries the launch URL in data-src only',
      state1.dataSrc === LAUNCH_URL,
      `data-src=${state1.dataSrc}`,
    );
    check(
      'patient was not moved off the dashboard',
      (await evaluate('location.href')).includes(DASHBOARD_MARKER),
    );
    check(
      'AC5 tile and accordion grouping unchanged',
      state1.tilePresent &&
        state1.tileToggle === 'collapse' &&
        state1.tileParent === '#cardgroup' &&
        state1.parent === '#cardgroup',
      JSON.stringify({
        tileToggle: state1.tileToggle,
        tileParent: state1.tileParent,
        panelParent: state1.parent,
      }),
    );
    await shot('case1-dashboard-render');

    // ------------------------------------------ case 2: opened by a mouse
    console.log('\n== case 2: patient opens the tile with the mouse ==');
    const t2 = Date.now();
    // With no AI session the launch reaches a login page whose TICK-045 breakout
    // hoists the top-level window off the dashboard within ~200ms, taking the panel's
    // DOM with it -- correct, and out of scope here, but it means the promotion has to
    // be recorded as it happens rather than sampled afterwards. sessionStorage is
    // per-origin-per-tab and the breakout's destination is the same origin as the
    // portal, so the record survives the hoist.
    await evaluate(`(() => {
      const panel = document.getElementById('aeai-portal-chat');
      const frame = panel.querySelector('iframe[data-aeai-portal-chat]');
      sessionStorage.removeItem('aeaiProbe');
      new MutationObserver(() => {
        sessionStorage.setItem('aeaiProbe', JSON.stringify({src: frame.getAttribute('src')}));
      }).observe(frame, {attributes: true, attributeFilter: ['src']});
    })()`);
    await clickTile(400);
    await sleep(4000);
    const promoted = JSON.parse((await evaluate("sessionStorage.getItem('aeaiProbe')")) || 'null');
    check(
      'the click promoted data-src to src',
      promoted !== null && promoted.src === LAUNCH_URL,
      promoted ? `src=${promoted.src}` : 'no src mutation was recorded',
    );
    const launch2 = launchRequests(t2);
    check(
      'AC2 opening the tile starts exactly one authorization',
      launch2.length === 1,
      `${launch2.length} requests to ${LAUNCH_URL}`,
    );
    // Not an acceptance criterion -- where the authorization lands is TICK-051's and
    // TICK-045's, both explicitly out of scope here. Asserted only to prove the launch
    // this ticket defers is a *real* one: it reaches OpenEMR's authorization server.
    const authorize2 = authorizeRequests(t2);
    check(
      'the deferred launch is a real authorization (it reaches OpenEMR)',
      authorize2.length >= 1,
      authorize2.length ? authorize2[0].url.slice(0, 90) : 'no /oauth2/default/ request followed',
    );
    console.log(`    top-level after opening: ${await evaluate('location.href')}`);
    await shot('case2-after-opening-the-tile');

    // ------------------------- case 3: returning patient / whereto restore
    console.log('\n== case 3: dashboard reload after the chat was used ==');
    const t3 = Date.now();
    await navigate(`https://emr.localhost/${DASHBOARD_MARKER}`);
    await sleep(3000);
    const state3 = await panelState();
    const chat3 = chatRequests(t3);
    check(
      'AC1 reload after using the chat still issues no request to the AI server',
      chat3.length === 0,
      `whereto=${state3.whereto}, ${chat3.length} ${CHAT_ORIGIN} requests`,
    );
    check(
      "the portal's own panel restore did not open the chat",
      state3.shown === false && state3.whereto === '#aeai-portal-chat',
      `panel .show=${state3.shown}, whereto=${state3.whereto}`,
    );
    check(
      'AC5 the cancelled restore still lands the patient on a usable dashboard',
      state3.dashboardShown === true && state3.tileVisible === true,
      `#quickstart-card .show=${state3.dashboardShown}, tile visible=${state3.tileVisible}`,
    );
    check(
      'patient was not moved off the reloaded dashboard',
      (await evaluate('location.href')).includes(DASHBOARD_MARKER),
    );
    await shot('case3-reload-after-use');

    // --------------------------------------- case 4: keyboard activation
    console.log('\n== case 4: patient opens the tile with the keyboard ==');
    const t4 = Date.now();
    const focusedId = await enterOnTile();
    const launch4 = launchRequests(t4);
    check(
      'AC2 Enter on the focused tile starts the authorization (NFR-19)',
      launch4.length === 1 && focusedId === 'aeai-chat-go',
      `focus=${focusedId}, ${launch4.length} requests to ${LAUNCH_URL}`,
    );
    await shot('case4-keyboard-open');

    // ------------------------ case 5: collapse and re-open does not reload
    console.log('\n== case 5: collapse and re-open keeps the same frame ==');
    stubLaunch = true;
    const t5 = Date.now();
    await navigate(`https://emr.localhost/${DASHBOARD_MARKER}`);
    await sleep(2000);
    check(
      'AC1 holds with the launch response stubbed too',
      chatRequests(t5).length === 0,
      `${chatRequests(t5).length} ${CHAT_ORIGIN} requests on render`,
    );
    await clickTile();
    const openState = await panelState();
    const afterFirstOpen = launchRequests(t5).length;
    check(
      'first open loads the chat once',
      afterFirstOpen === 1 && openState.shown && openState.src === LAUNCH_URL,
      `${afterFirstOpen} requests, shown=${openState.shown}`,
    );
    // How a patient actually leaves the chat: the portal's own Dashboard button. The
    // AI Chat tile lives inside `#quickstart-card`, which the `#cardgroup` accordion
    // collapsed when the chat opened, so the tile is not on screen to click again --
    // this is the "collapse the panel to check appointments and come back" path.
    await clickElement('quickstart_dashboard');
    const collapsed = await panelState();
    check(
      'leaving the chat via the Dashboard button collapses the panel',
      collapsed.shown === false && collapsed.dashboardShown === true,
      `chat .show=${collapsed.shown}, #quickstart-card .show=${collapsed.dashboardShown}`,
    );
    await clickTile();
    const reopened = await panelState();
    const afterReopen = launchRequests(t5).length;
    check(
      'AC3 re-opening the panel did not reload the iframe',
      afterReopen === 1,
      `${afterReopen} requests to ${LAUNCH_URL} across open -> collapse -> open`,
    );
    check(
      'AC3 the panel is open again with the same src',
      reopened.shown && reopened.src === LAUNCH_URL,
      `shown=${reopened.shown} src=${reopened.src}`,
    );
    await shot('case5-reopened-without-reload');

    // ------------------------------------- case 6: AI server is the only origin
    console.log('\n== case 6: what the deferred load put in the DOM ==');
    const domScan = JSON.parse(
      await evaluate(`(() => {
        const panel = document.getElementById('aeai-portal-chat');
        const html = panel.outerHTML;
        const attrs = [];
        panel.querySelectorAll('*').forEach((el) => {
          for (const a of el.attributes) attrs.push(a.name + '=' + a.value);
        });
        return JSON.stringify({html: html, attrs: attrs});
      })()`),
    );
    const forbidden = ['Bearer', 'access_token', 'id_token', 'patient=', 'pid=', 'csrf'];
    const hits = forbidden.filter((f) => domScan.html.includes(f));
    check(
      'AC4 no token or patient identifier entered the DOM',
      hits.length === 0,
      hits.length ? `found ${hits.join(', ')}` : 'panel subtree carries only the launch URL',
    );
    const foreign = domScan.attrs.filter(
      (a) => /https?:\/\//.test(a) && !a.includes(CHAT_ORIGIN),
    );
    check(
      'AC4 the AI server is the panel subtree\'s only network target',
      foreign.length === 0,
      foreign.length ? foreign.join(' | ') : 'only chat.localhost',
    );

    if (failures.length) {
      console.log(`\n    network failures observed: ${[...new Set(failures)].join(', ')}`);
    }
    console.log(`\n    total requests captured: ${requests.length}`);
    console.log(`    requests to ${CHAT_ORIGIN}: ${chatRequests().length}`);
    for (const r of chatRequests()) console.log(`      ${new Date(r.at).toISOString()}  ${r.url}`);
  } finally {
    chrome.kill('SIGKILL');
    rmSync(profile, { recursive: true, force: true });
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    console.log(`FAILED: ${failed.map((r) => r.name).join('; ')}`);
    process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
