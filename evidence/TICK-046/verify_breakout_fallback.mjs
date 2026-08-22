// TICK-046 live verification harness: drives real Chrome (via the DevTools
// protocol over Node's built-in WebSocket -- no new project dependency) against
// the *actual* iframe-breakout `<script>` extracted verbatim from
// openemr_overrides/templates/oauth2/oauth2-login.html.twig.
//
// Two cases, both in a real browser:
//
//   blocked  parent page and embedded page on different origins (the
//            chat.localhost-vs-emr.localhost topology drift TICK-045's own live
//            verification hit). Chrome silently refuses the gesture-less
//            top-level navigation, so the fallback must appear and, when
//            actually clicked, must navigate the top frame.
//   normal   parent page and embedded page same-origin (production today).
//            The top frame must still navigate straight to the login page and
//            the fallback must never render.
//
// Run: node evidence/TICK-046/verify_breakout_fallback.mjs
// Exits non-zero if any assertion fails. Starts nothing that outlives it.

import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PORT = 8917;
// Same server, two hostnames -> two origins, exactly like emr.localhost vs
// chat.localhost. `localhost` hosts the page under test in both cases.
const CHILD_HOST = 'localhost';
const CROSS_ORIGIN_PARENT_HOST = '127.0.0.1';
const CHROME =
  '/Users/megalodon/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/' +
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const IFRAME_TOP = 100;
const IFRAME_LEFT = 50;
const IFRAME_WIDTH = 420;

const TEMPLATE = new URL(
  '../../openemr_overrides/templates/oauth2/oauth2-login.html.twig',
  import.meta.url,
);

/** The shipped breakout script, lifted out of the Twig template verbatim. */
function shippedBreakoutScript() {
  const text = readFileSync(TEMPLATE, 'utf8');
  const start = text.indexOf('{% block scripts %}');
  const end = text.indexOf('{% endblock %}', start);
  const block = text.slice(start + '{% block scripts %}'.length, end);
  const script = block.replace(/\{#[\s\S]*?#\}/g, '').trim();
  if (!script.startsWith('<script>') || !script.endsWith('</script>')) {
    throw new Error('could not extract the breakout <script> from the template');
  }
  return script;
}

const reports = [];

function parentHtml(childUrl) {
  return `<!doctype html><html><head><title>Portal host page</title></head>
<body style="margin:0;font:14px sans-serif">
<div id="host-marker">PORTAL HOST PAGE (top frame)</div>
<iframe src="${childUrl}" title="AI Chat"
  style="position:absolute;top:${IFRAME_TOP}px;left:${IFRAME_LEFT}px;width:${IFRAME_WIDTH}px;height:640px;border:0"></iframe>
</body></html>`;
}

function childHtml(caseName, script) {
  // The shipped script first, then harness-only observation code that reports
  // what the shipped script did back to this server.
  return `<!doctype html><html><head><title>OpenEMR Authorization</title>
${script}
<script>
  (function () {
    var report = function (state, extra) {
      var payload = Object.assign({ case: ${JSON.stringify(caseName)}, state: state }, extra || {});
      navigator.sendBeacon('/report', JSON.stringify(payload));
    };
    report('child-loaded', { embedded: window.top !== window.self });
    var seen = false;
    setInterval(function () {
      var el = document.getElementById('aeai-breakout-fallback');
      if (!el || seen) { return; }
      seen = true;
      var rect = el.getBoundingClientRect();
      var style = window.getComputedStyle(el);
      report('fallback-rendered', {
        text: (el.textContent || '').trim(),
        href: el.getAttribute('href'),
        target: el.getAttribute('target'),
        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
        display: style.display,
        visibility: style.visibility,
        opacity: style.opacity
      });
    }, 100);
  })();
</script></head>
<body style="margin:0;font:14px sans-serif">
<div id="login-marker">EMBEDDED SIGN-IN FORM (unusable inside the 640px panel)</div>
</body></html>`;
}

function startServer(script) {
  const server = createServer((req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname === '/report') {
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        reports.push(JSON.parse(body));
        res.writeHead(204).end();
      });
      return;
    }
    if (url.pathname === '/parent') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(parentHtml(url.searchParams.get('child')));
      return;
    }
    if (url.pathname === '/login') {
      res.writeHead(200, { 'content-type': 'text/html' });
      res.end(childHtml(url.searchParams.get('case'), script));
      return;
    }
    res.writeHead(404).end();
  });
  return new Promise((resolve) => server.listen(PORT, () => resolve(server)));
}

/** Minimal CDP client over Node's global WebSocket. */
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      const resolver = this.pending.get(msg.id);
      if (resolver) {
        this.pending.delete(msg.id);
        msg.error ? resolver.reject(new Error(JSON.stringify(msg.error))) : resolver.resolve(msg.result);
      }
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

  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` -- ${detail}` : ''}`);
}

async function main() {
  const script = shippedBreakoutScript();
  console.log('--- breakout script under test (verbatim from oauth2-login.html.twig) ---');
  console.log(script);
  console.log('---\n');

  const server = await startServer(script);
  const profile = mkdtempSync(join(tmpdir(), 'tick046-'));
  const debugPort = 9333;
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--window-size=1200,900',
  ]);

  try {
    const browser = await Cdp.connect(await chromeWsUrl(debugPort));

    for (const [caseName, parentHost] of [
      ['blocked', CROSS_ORIGIN_PARENT_HOST],
      ['normal', CHILD_HOST],
    ]) {
      const childUrl = `http://${CHILD_HOST}:${PORT}/login?case=${caseName}`;
      const parentUrl = `http://${parentHost}:${PORT}/parent?child=${encodeURIComponent(childUrl)}`;
      const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
      const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
      const evaluate = async (expression) =>
        (await browser.send('Runtime.evaluate', { expression, returnByValue: true }, sessionId)).result.value;

      console.log(`\n### case "${caseName}": top=${parentUrl}\n    iframe=${childUrl}`);
      await browser.send('Page.enable', {}, sessionId);
      await browser.send('Page.navigate', { url: parentUrl }, sessionId);
      await sleep(3500);

      const topUrl = await evaluate('location.href');
      const topTitle = await evaluate('document.title');
      const loaded = reports.find((r) => r.case === caseName && r.state === 'child-loaded');
      const fallback = reports.find((r) => r.case === caseName && r.state === 'fallback-rendered');
      console.log(`    top frame is now: ${topUrl} (title "${topTitle}")`);
      console.log(`    child reported:   ${JSON.stringify(loaded)}`);
      console.log(`    fallback:         ${JSON.stringify(fallback) || 'never rendered'}`);

      if (caseName === 'blocked') {
        check('blocked: embedded page loaded inside the iframe', loaded?.embedded === true);
        check(
          'blocked: Chrome silently refused the top-level navigation',
          topUrl === parentUrl,
          `top stayed at ${topUrl}`,
        );
        check(
          'blocked: a visible, actionable fallback appeared',
          !!fallback &&
            fallback.text === 'Click here to sign in' &&
            fallback.target === '_top' &&
            fallback.href === childUrl &&
            fallback.display !== 'none' &&
            fallback.visibility === 'visible' &&
            fallback.rect.width > 100 &&
            fallback.rect.height > 10,
          fallback ? `"${fallback.text}" ${fallback.rect.width}x${fallback.rect.height}px` : 'absent',
        );

        // Without this, a missing fallback throws a TypeError on the next line
        // and the run dies on an unhandled rejection instead of reporting the
        // FAIL above and exiting non-zero.
        if (!fallback) continue;

        // Click it for real, through Chrome's own input pipeline, at the
        // fallback's position in the top frame's viewport.
        const x = IFRAME_LEFT + fallback.rect.x + fallback.rect.width / 2;
        const y = IFRAME_TOP + fallback.rect.y + fallback.rect.height / 2;
        for (const type of ['mousePressed', 'mouseReleased']) {
          await browser.send(
            'Input.dispatchMouseEvent',
            { type, x, y, button: 'left', clickCount: 1 },
            sessionId,
          );
        }
        await sleep(2000);
        const afterClick = await evaluate('location.href');
        console.log(`    after clicking the fallback at (${x}, ${y}): ${afterClick}`);
        check(
          'blocked: clicking the fallback navigates the top frame to the sign-in page',
          afterClick === childUrl,
          afterClick,
        );
        check(
          'blocked: the embedded panel is gone -- sign-in is now the whole page',
          (await evaluate('!!document.getElementById("login-marker")')) === true &&
            (await evaluate('!!document.getElementById("host-marker")')) === false,
        );
      } else {
        check(
          'normal: the same-origin breakout still navigates the top frame',
          topUrl === childUrl,
          topUrl,
        );
        check('normal: no fallback was ever shown to the patient', !fallback);
        check(
          'normal: sign-in page is top-level, host page gone',
          (await evaluate('!!document.getElementById("login-marker")')) === true &&
            (await evaluate('!!document.getElementById("host-marker")')) === false,
        );
      }
      await browser.send('Target.closeTarget', { targetId });
    }
  } finally {
    chrome.kill('SIGKILL');
    server.close();
    rmSync(profile, { recursive: true, force: true });
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

await main();
