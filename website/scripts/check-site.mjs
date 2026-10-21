#!/usr/bin/env node
/**
 * Smoke-checks the BUILT site (run `npm run build` first): serves build/ on a private port, opens
 * a set of pages at desktop and phone width in light and dark mode, and fails on
 *   - horizontal page overflow (content wider than the viewport),
 *   - console errors / failed same-origin requests,
 *   - missing screenshots (images that did not load).
 * Screenshots of every page land in <repo>/.artifacts/check-site/ (gitignored; override with
 * CHECK_SITE_OUT) for a visual review.
 *
 *   npm run check:site                 # default page list
 *   npm run check:site -- /docs/intro  # specific routes
 */
import {spawn} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '@playwright/test';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const out = resolve(process.env.CHECK_SITE_OUT ?? resolve(site, '../.artifacts/check-site'));
mkdirSync(out, {recursive: true});
const PORT = Number(process.env.CHECK_SITE_PORT ?? 8868);
const BASE = (process.env.DOCS_BASE_URL ?? '/autoair/').replace(/\/$/, '');
const ROUTES =
  process.argv.slice(2).length > 0
    ? process.argv.slice(2)
    : [
        '/',
        '/docs/intro',
        '/docs/getting-started/first-dag',
        '/docs/installation/extras',
        '/docs/user-guide/canvas',
        '/docs/user-guide/shortcuts',
        '/docs/user-guide/deploy',
        '/docs/ai/introduction',
        '/docs/admin/iam',
        '/docs/admin/configuration',
        '/docs/admin/feature-flags',
        '/docs/deployment/kubernetes',
        '/docs/reference/configuration',
        '/blog',
        '/api',
        '/search?q=deploy',
      ];
const VIEWPORTS = [
  {name: 'desktop-1280', width: 1280, height: 800},
  {name: 'desktop', width: 1440, height: 900},
  {name: 'desktop-1920', width: 1920, height: 1080},
  {name: 'mobile', width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2},
];

const server = spawn(resolve(site, 'node_modules/.bin/docusaurus'), ['serve', '--port', String(PORT), '--no-open'], {cwd: site, stdio: 'ignore'});
const stop = () => server.kill();
process.on('exit', stop);

async function waitUp() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://localhost:${PORT}${BASE}/`);
      if (r.ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('docusaurus serve did not start');
}

await waitUp();
const browser = await chromium.launch();
const problems = [];
for (const vp of VIEWPORTS) {
  for (const scheme of ['light', 'dark']) {
    const ctx = await browser.newContext({...vp, viewport: {width: vp.width, height: vp.height}, colorScheme: scheme});
    const page = await ctx.newPage();
    const errors = [];
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
    page.on('requestfailed', (r) => r.url().includes(`localhost:${PORT}`) && !/ERR_ABORTED/.test(r.failure()?.errorText ?? '') && errors.push(`failed: ${r.url()} ${r.failure()?.errorText}`));
    for (const route of ROUTES) {
      errors.length = 0;
      await page.goto(`http://localhost:${PORT}${BASE}${route}`, {waitUntil: 'networkidle'});
      await page.waitForTimeout(route === '/api' ? 2500 : 300);
      const m = await page.evaluate(() => {
        const w = document.documentElement.clientWidth;
        const wide = [...document.querySelectorAll('body *')]
          .filter((el) => el.getBoundingClientRect().right > w + 1 && getComputedStyle(el).position !== 'fixed')
          .filter((el) => !el.closest('pre, table, .docusaurus-mermaid-container, .theme-code-block, .aa-redoc, .navbar-sidebar, .tabs'))
          .slice(0, 3)
          .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`);
        const broken = [...document.images].filter((i) => i.complete && i.naturalWidth === 0 && i.offsetParent).map((i) => i.src);
        return {overflow: document.documentElement.scrollWidth - w, wide, broken};
      });
      const tag = `${vp.name}/${scheme} ${route}`;
      if (m.overflow > 1) problems.push(`${tag}: page overflows by ${m.overflow}px (${m.wide.join(', ')})`);
      for (const b of m.broken) problems.push(`${tag}: image did not load ${b}`);
      for (const e of errors.filter((e) => !/cdn\.jsdelivr|favicon/.test(e))) problems.push(`${tag}: ${e}`);
      const file = `${vp.name}-${scheme}-${route.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'home'}.png`;
      await page.screenshot({path: resolve(out, file), fullPage: vp.name === 'mobile' ? false : false});
    }
    await ctx.close();
  }
}
await browser.close();
stop();
console.log(`check-site: ${ROUTES.length} routes x ${VIEWPORTS.length} viewports x 2 modes; screenshots in ${out}`);
if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log('check-site: no problems');
