#!/usr/bin/env node
/**
 * Copies the generated references of the repository into the docs site, so the site can
 * never drift from the code:
 *
 *   docs/configuration-reference.md  ->  website/docs/reference/configuration.md
 *
 * The source is generated from the code (scripts/gen_config_docs.py) and a test fails when it
 * is stale. This script only adds
 * front matter, drops the H1 (the title comes from the front matter) and rewrites links
 * between repository docs to site routes. It runs before `npm start` and `npm run build`.
 */
import {mkdirSync, readFileSync, writeFileSync, existsSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repoDocs = resolve(here, '../../docs');
const outDir = resolve(here, '../docs/reference');

/** Repository doc file -> site route. Unknown .md links fail the sync loudly. */
const LINKS = {
  'configuration.md': '/docs/admin/configuration',
  'configuration-reference.md': '/docs/reference/configuration',
  'deployment.md': '/docs/installation/standalone',
  'iam.md': '/docs/admin/iam',
  'sso.md': '/docs/admin/sso',
  'mcp.md': '/docs/ai/mcp',
  'ai-architecture.md': '/docs/ai/overview',
  'ai-assistants.md': '/docs/ai/assistants',
  'ai-troubleshooting.md': '/docs/ai/troubleshooting',
  'extending.md': '/docs/extending/overview',
  'architecture/state-and-scaling.md': '/docs/admin/scaling',
  'policy.md': '/docs/admin/policy',
  'ROADMAP.md': '/docs/roadmap',
};

const PAGES = [
  {
    source: 'configuration-reference.md',
    target: 'configuration.md',
    frontMatter: {
      id: 'configuration',
      title: 'Configuration reference',
      sidebar_label: 'Configuration reference',
      description:
        'Every AutoAir setting: environment variable, configuration-file key, type, default, description and secret flag.',
      toc_max_heading_level: 2,
      pagination_next: 'reference/cli',
    },
    transform: stackNameColumns,
    intro:
      ':::info Generated page\n\nThis page is generated from the `Settings` model ' +
      '(`scripts/gen_config_docs.py` writes `docs/configuration-reference.md`, and the docs ' +
      'build copies it here). Do not edit it in the website; change the setting\'s ' +
      'description in `src/autoair` instead. How sources and precedence work is explained in ' +
      '[Configuration](/docs/admin/configuration).\n\n:::\n',
  },
];

/**
 * The configuration tables have six columns, and two of them hold long names that must not
 * break (AUTOAIR_AI__TROUBLESHOOT__FILESYSTEM__INCLUDE_DAGS_FOLDER). On the site the variable
 * and its configuration-file key share one cell, one above the other, which leaves the
 * description a readable width.
 */
const CONFIG_HEADER = '| Environment variable | Config key | Type | Default | Description | Secret |';
function stackNameColumns(text) {
  let inTable = false;
  return text
    .split('\n')
    .map((line) => {
      if (line === CONFIG_HEADER) {
        inTable = true;
        return '| Environment variable<br/>Config key | Type | Default | Description | Secret |';
      }
      if (!inTable) return line;
      if (!line.startsWith('|')) {
        inTable = false;
        return line;
      }
      if (/^\|(-+\|)+$/.test(line)) return '|---|---|---|---|---|';
      const cells = line.split(/(?<!\\)\|/).slice(1, -1);
      if (cells.length !== 6) throw new Error(`configuration table row with ${cells.length} cells: ${line}`);
      const [env, key, ...rest] = cells.map((c) => c.trim());
      return `| ${env}<br/>${key} | ${rest.join(' | ')} |`;
    })
    .join('\n');
}

function yamlValue(v) {
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

function rewriteLinks(text, source) {
  return text.replace(/\]\(([^)\s]+?\.md)(#[^)\s]*)?\)/g, (match, file, anchor = '') => {
    const route = LINKS[file];
    if (!route) {
      throw new Error(`${source}: no site route for link target "${file}" (add it to LINKS)`);
    }
    return `](${route}${anchor})`;
  });
}

let changed = 0;
mkdirSync(outDir, {recursive: true});
for (const page of PAGES) {
  const src = resolve(repoDocs, page.source);
  if (!existsSync(src)) {
    console.error(`sync-reference: ${src} is missing`);
    process.exit(1);
  }
  let body = readFileSync(src, 'utf8').replace(/\r\n/g, '\n');
  body = body.replace(/^# .*\n+/, ''); // the front matter title replaces the H1
  body = rewriteLinks(body, page.source);
  if (page.transform) body = page.transform(body);
  const fm = Object.entries(page.frontMatter)
    .map(([k, v]) => `${k}: ${yamlValue(v)}`)
    .join('\n');
  const out =
    `---\n${fm}\ncustom_edit_url: null\n---\n\n` +
    `<!-- Generated from docs/${page.source} by website/scripts/sync-reference.mjs. Do not edit. -->\n\n` +
    `${page.intro}\n${body}`;
  const target = resolve(outDir, page.target);
  const before = existsSync(target) ? readFileSync(target, 'utf8') : '';
  if (before !== out) {
    writeFileSync(target, out);
    changed += 1;
  }
}
console.log(`sync-reference: ${PAGES.length} reference pages in sync (${changed} updated)`);
