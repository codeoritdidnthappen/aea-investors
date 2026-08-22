// TICK-046 live verification, part 2: the *real* local deployment.
//
// Same two cases as verify_breakout_fallback.mjs, but against the running
// OpenEMR/ai-server stack instead of a synthetic page: real Chrome loads a real
// portal host page and embeds the real `https://chat.localhost/oauth/launch`
// iframe, whose redirect chain lands on the real OpenEMR OAuth2 login page.
//
// The running container bind-mounts the OAuth2 template overrides from the main
// repo checkout, not from this build worktree, so it still serves the
// pre-TICK-046 script. Rather than mutate the container or the main checkout
// (the mount hazard recorded in evidence/TICK-045/LIVE_VERIFICATION_2026-08-21.md),
// this intercepts the real login response and swaps the old breakout script for
// this worktree's patched one -- byte for byte the HTML the container will
// serve once this ticket merges.
//
//   normal   host page on https://emr.localhost (production topology: the
//            portal page and the login page are same-origin).
//   blocked  host page on https://chat.localhost -- the exact cross-origin
//            topology TICK-045's live verification accidentally hit, where
//            Chrome silently refuses the top-level navigation.
//
// Run: node evidence/TICK-046/verify_breakout_fallback_live_app.mjs
// Exits non-zero if any assertion fails. Starts nothing that outlives it.

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME =
  '/Users/megalodon/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/' +
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const EMR_HOST_PAGE = 'https://emr.localhost/portal/index.php?site=default';
const CHAT_HOST_PAGE = 'https://chat.localhost/health';
const LAUNCH_URL = 'https://chat.localhost/oauth/launch';
const LOGIN_URL_MARKER = 'oauth2/default/provider/login';
const IFRAME_TOP = 120;
const IFRAME_LEFT = 40;
const IFRAME_WIDTH = 480;

const TEMPLATE = new URL(
  '../../openemr_overrides/templates/oauth2/oauth2-login.html.twig',
  import.meta.url,
);
const OLD_SCRIPT_BODY = `if (window.top !== window.self) {
            window.top.location.href = window.location.href;
        }`;

/** The patched breakout script body, lifted out of the Twig template verbatim. */
function patchedScriptBody() {
  const text = readFileSync(TEMPLATE, 'utf8');
  const start = text.indexOf('        if (window.top !== window.self) {');
  const end = text.indexOf('\n    </script>', start);
  const body = text.slice(start, end).trim();
  if (!body.includes('aeai-breakout-fallback')) {
    throw new Error('could not extract the patched breakout script from the template');
  }
  return body;
}

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

async function evaluateIn(browser, sessionId, expression) {
  const res = await browser.send(
    'Runtime.evaluate',
    { expression, returnByValue: true },
    sessionId,
  );
  return res.result.value;
}

/** The attached target whose document is the OAuth2 login page. */
async function findLoginSession(browser, sessions) {
  for (const sid of sessions.keys()) {
    try {
      const url = await evaluateIn(browser, sid, 'location.href');
      if (typeof url === 'string' && url.includes(LOGIN_URL_MARKER)) return sid;
    } catch {
      /* detached or not a page target */
    }
  }
  throw new Error('no attached target is showing the OAuth2 login page');
}

async function screenshot(browser, sessionId, name) {
  const { data } = await browser.send('Page.captureScreenshot', { format: 'png' }, sessionId);
  const path = new URL(`./screenshot-${name}.png`, import.meta.url);
  writeFileSync(path, Buffer.from(data, 'base64'));
  console.log(`    screenshot: evidence/TICK-046/screenshot-${name}.png`);
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -- ${detail}` : ''}`);
}

async function main() {
  const patched = patchedScriptBody();
  const profile = mkdtempSync(join(tmpdir(), 'tick046-live-'));
  const debugPort = 9334;
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    '--ignore-certificate-errors',
    '--no-first-run',
    '--window-size=1200,900',
  ]);

  const patchedLoginUrls = [];
  try {
    const browser = await Cdp.connect(await chromeWsUrl(debugPort));
    const sessions = new Map();

    // Attach to every frame target (chat.localhost iframes are out-of-process)
    // and intercept the OAuth2 login response wherever it lands.
    browser.on(async (msg) => {
      if (msg.method === 'Target.attachedToTarget') {
        const sid = msg.params.sessionId;
        sessions.set(sid, msg.params.targetInfo.url);
        try {
          await browser.send(
            'Target.setAutoAttach',
            { autoAttach: true, waitForDebuggerOnStart: true, flatten: true },
            sid,
          );
          await browser.send(
            'Fetch.enable',
            { patterns: [{ urlPattern: '*', requestStage: 'Response' }] },
            sid,
          );
        } catch {
          /* target may already be gone */
        }
        try {
          await browser.send('Runtime.runIfWaitingForDebugger', {}, sid);
        } catch {
          /* not paused */
        }
      }
      if (msg.method === 'Fetch.requestPaused') {
        const { requestId, request, responseStatusCode, responseHeaders } = msg.params;
        const sid = msg.sessionId;
        if (!request.url.includes(LOGIN_URL_MARKER) || responseStatusCode >= 300) {
          try {
            await browser.send('Fetch.continueRequest', { requestId }, sid);
          } catch {
            /* raced with navigation */
          }
          return;
        }
        try {
          const { body, base64Encoded } = await browser.send(
            'Fetch.getResponseBody',
            { requestId },
            sid,
          );
          const html = base64Encoded ? Buffer.from(body, 'base64').toString('utf8') : body;
          if (!html.includes(OLD_SCRIPT_BODY)) {
            console.log(
              `    (not patched: ${request.method} ${request.url} -> ${html.length} bytes, ` +
                `breakout script present: ${html.includes('window.top !== window.self')})`,
            );
            await browser.send('Fetch.continueRequest', { requestId }, sid);
            return;
          }
          const swapped = html.replace(OLD_SCRIPT_BODY, patched);
          patchedLoginUrls.push(request.url);
          console.log(`    intercepted + patched real login response: ${request.url}`);
          await browser.send(
            'Fetch.fulfillRequest',
            {
              requestId,
              responseCode: responseStatusCode,
              responseHeaders: (responseHeaders || []).filter(
                (h) => !['content-length', 'content-encoding'].includes(h.name.toLowerCase()),
              ),
              body: Buffer.from(swapped, 'utf8').toString('base64'),
            },
            sid,
          );
        } catch (err) {
          console.log(`    !! interception error: ${err.message}`);
        }
      }
    });

    await browser.send('Target.setDiscoverTargets', { discover: true });
    await browser.send('Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: true,
      flatten: true,
    });

    for (const [caseName, hostPage] of [
      ['normal', EMR_HOST_PAGE],
      ['blocked', CHAT_HOST_PAGE],
    ]) {
      const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
      const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
      const evaluate = async (expression) => {
        const res = await browser.send(
          'Runtime.evaluate',
          { expression, returnByValue: true, awaitPromise: true },
          sessionId,
        );
        return res.result.value;
      };

      // Per-case, so "the patched login page was the one served" cannot be
      // satisfied by an interception from the previous case.
      patchedLoginUrls.length = 0;

      console.log(`\n### real-app case "${caseName}": host page ${hostPage}`);
      await browser.send('Page.enable', {}, sessionId);
      await browser.send('Page.navigate', { url: hostPage }, sessionId);
      await sleep(2500);
      console.log(`    host page loaded: ${await evaluate('location.href')}`);

      // Embed the real chat panel iframe, same src as PortalChatController emits.
      await evaluate(`(function () {
        var f = document.createElement('iframe');
        f.title = 'AI Chat';
        f.setAttribute('data-aeai-portal-chat', '1');
        f.src = ${JSON.stringify(LAUNCH_URL)};
        f.setAttribute('style', 'position:absolute;top:${IFRAME_TOP}px;left:${IFRAME_LEFT}px;'
          + 'width:${IFRAME_WIDTH}px;min-height:640px;border:0;background:#fff');
        document.body.appendChild(f);
        return true;
      })()`);
      await sleep(6000);

      const topUrl = await evaluate('location.href');
      const topTitle = await evaluate('document.title');
      console.log(`    top frame is now: ${topUrl}\n    title: "${topTitle}"`);

      if (caseName === 'normal') {
        check(
          'real app / same-origin: the breakout still navigates the top frame to the login page',
          topUrl.includes(LOGIN_URL_MARKER),
          topUrl,
        );
        check(
          'real app / same-origin: patched login page was the one served',
          patchedLoginUrls.length > 0,
          `${patchedLoginUrls.length} patched response(s)`,
        );
        check(
          'real app / same-origin: no fallback shown -- the normal path is unchanged',
          (await evaluate('!!document.getElementById("aeai-breakout-fallback")')) === false,
        );
        check(
          'real app / same-origin: a real full-page Sign In form is present',
          (await evaluate('!!document.querySelector("form#userLogin")')) === true,
        );
      } else {
        check(
          'real app / cross-origin: Chrome silently refused the top-level navigation',
          !topUrl.includes(LOGIN_URL_MARKER),
          topUrl,
        );
        check(
          'real app / cross-origin: patched login page was the one served',
          patchedLoginUrls.length > 0,
          `${patchedLoginUrls.length} patched response(s)`,
        );

        // The login page is cross-origin with the top frame here (that is the
        // whole point), so read the fallback from the login frame's own target.
        const loginSession = await findLoginSession(browser, sessions);
        const fallback = await evaluateIn(
          browser,
          loginSession,
          `(function () {
            var el = document.getElementById('aeai-breakout-fallback');
            if (!el) { return null; }
            var rect = el.getBoundingClientRect();
            var style = getComputedStyle(el);
            return {
              text: (el.textContent || '').trim(),
              href: el.getAttribute('href'),
              target: el.getAttribute('target'),
              display: style.display,
              visibility: style.visibility,
              rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
              stillEmbedded: window.top !== window.self,
              signInFormBehindIt: !!document.querySelector('form#userLogin')
            };
          })()`,
        );
        console.log(`    login frame reports: ${JSON.stringify(fallback)}`);
        check(
          'real app / cross-origin: the fallback is visible in the trapped login panel',
          !!fallback &&
            fallback.stillEmbedded === true &&
            fallback.text === 'Click here to sign in' &&
            fallback.target === '_top' &&
            fallback.href.includes(LOGIN_URL_MARKER) &&
            fallback.display !== 'none' &&
            fallback.visibility === 'visible' &&
            fallback.rect.width > 100 &&
            fallback.rect.height > 10,
          fallback ? `"${fallback.text}" ${fallback.rect.width}x${fallback.rect.height}px` : 'absent',
        );
        await screenshot(browser, sessionId, 'blocked-fallback-visible');

        // Without this, a missing fallback throws a TypeError on the next line
        // and the run dies on an unhandled rejection instead of reporting the
        // FAIL above and exiting non-zero.
        if (!fallback) continue;

        // Click it for real, through Chrome's own input pipeline.
        const x = IFRAME_LEFT + fallback.rect.x + fallback.rect.width / 2;
        const y = IFRAME_TOP + fallback.rect.y + fallback.rect.height / 2;
        for (const type of ['mousePressed', 'mouseReleased']) {
          await browser.send(
            'Input.dispatchMouseEvent',
            { type, x, y, button: 'left', clickCount: 1 },
            sessionId,
          );
        }
        await sleep(4000);
        const afterClick = await evaluate('location.href');
        console.log(`    after clicking the fallback at (${x}, ${y}): ${afterClick}`);
        check(
          'real app / cross-origin: clicking the fallback escapes the panel to a full-page sign in',
          afterClick.includes(LOGIN_URL_MARKER) &&
            (await evaluate('!!document.querySelector("form#userLogin")')) === true &&
            (await evaluate('!!document.querySelector("iframe[data-aeai-portal-chat]")')) === false,
          afterClick,
        );
        await screenshot(browser, sessionId, 'blocked-after-fallback-click');
      }

      if (caseName === 'normal') {
        await screenshot(browser, sessionId, 'normal-breakout-top-level');
      }
      await browser.send('Target.closeTarget', { targetId });
    }
  } finally {
    chrome.kill('SIGKILL');
    rmSync(profile, { recursive: true, force: true });
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

await main();
