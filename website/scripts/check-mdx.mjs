#!/usr/bin/env node
/**
 * Fast syntax check for docs pages without a full site build: compiles every .mdx file (or the
 * files given as arguments) with MDX and every .md file as CommonMark, and checks that each
 * <Screenshot name="..."> / <Video name="..."> refers to an existing capture.
 *
 *   node scripts/check-mdx.mjs                # all of docs/
 *   node scripts/check-mdx.mjs docs/ai/*.mdx  # some files
 *
 * Links and anchors are only checked by `npm run build` (onBrokenLinks: 'throw').
 */
import {compile} from '@mdx-js/mdx';
import remarkGfm from 'remark-gfm';
import {existsSync, readFileSync, readdirSync, statSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const walk = (d) =>
  readdirSync(d).flatMap((f) => {
    const p = join(d, f);
    return statSync(p).isDirectory() ? walk(p) : /\.mdx?$/.test(f) ? [p] : [];
  });
const files = process.argv.length > 2 ? process.argv.slice(2).map((f) => resolve(f)) : walk(join(site, 'docs'));
let failed = 0;
const missing = new Set();
for (const file of files) {
  const raw = readFileSync(file, 'utf8');
  const body = raw.replace(/^---\n[\s\S]*?\n---\n/, '');
  try {
    await compile(body, {format: file.endsWith('.mdx') ? 'mdx' : 'md', remarkPlugins: [remarkGfm]});
  } catch (e) {
    failed += 1;
    console.error(`${file}: ${e.message}`);
  }
  if (file.endsWith('.md') && /<(Screenshot|Video)\b/.test(body)) {
    failed += 1;
    console.error(`${file}: uses <Screenshot>/<Video> but is .md (rename to .mdx)`);
  }
  for (const m of body.matchAll(/<Screenshot[^>]*\bname="([^"]+)"/g)) {
    for (const mode of ['light', 'dark']) {
      if (!existsSync(join(site, 'static/img/screens', `${m[1]}-${mode}.png`))) missing.add(`img/screens/${m[1]}-${mode}.png`);
    }
  }
  for (const m of body.matchAll(/<Video[^>]*\bname="([^"]+)"/g)) {
    if (!existsSync(join(site, 'static/video', `${m[1]}.webm`))) missing.add(`video/${m[1]}.webm`);
  }
}
if (missing.size) console.warn(`missing captures (run npm run capture):\n  ${[...missing].sort().join('\n  ')}`);
console.log(`check-mdx: ${files.length} files, ${failed} with errors, ${missing.size} missing captures`);
process.exit(failed || (process.env.STRICT_CAPTURES && missing.size) ? 1 : 0);
