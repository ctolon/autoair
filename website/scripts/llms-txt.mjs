/**
 * Docusaurus plugin that writes llms.txt and llms-full.txt (https://llmstxt.org) to the site
 * root after `npm run build`:
 *
 *   build/llms.txt       the index: title, a summary, then one section per top-level sidebar
 *                        category with a link and a one-line description for every page
 *   build/llms-full.txt  the full text of every docs page in sidebar order, as Markdown with
 *                        MDX/JSX removed and screenshots and videos replaced by their text
 *
 * Page order, titles and descriptions come from the docs plugin's loaded content, so the files
 * follow sidebars.ts and the pages' front matter. Registered in docusaurus.config.ts.
 */
import {readFileSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';

/** Words of the summary blockquote and the introduction under it. */
const SUMMARY =
  'AutoAir is a visual DAG builder for Apache Airflow 3. DAGs are drawn on a canvas or written ' +
  'in code mode, validated, and saved as readable Python that Airflow loads like any ' +
  'hand-written DAG. It runs inside the Airflow API server as a plugin or as a standalone ' +
  'server, deploys to the DAGs folder, Git (pull requests or direct pushes) or object storage, ' +
  'and authorizes every request through Airflow.';
const INTRO =
  'The documentation covers installation (pip extras, the Airflow plugin, the standalone ' +
  'server, Docker Compose and Helm), the builder, the optional AI features and MCP server, ' +
  'administration (configuration, IAM, SSO, policy, audit), deployment guides, extension ' +
  'points and the generated configuration and CLI references. Every page is also available ' +
  'as Markdown in llms-full.txt, and coding agents can search and read the documentation of ' +
  'the installed version over MCP with `autoair docs-mcp` (AutoAir docs MCP page below).';

function stripFrontMatter(text) {
  return text.replace(/^---\n[\s\S]*?\n---\n/, '');
}

/** JSX attribute value of `name` in a tag's source, or undefined. */
function attr(tag, name) {
  const m = tag.match(new RegExp(`\\b${name}=(?:"([^"]*)"|'([^']*)'|\\{["'\`]([^"'\`]*)["'\`]\\})`));
  return m ? (m[1] ?? m[2] ?? m[3]) : undefined;
}

/** Markdown text of an MDX page: components become text, MDX-only syntax is removed. */
function toMarkdown(source, absolute) {
  const out = [];
  let fence = null;
  const lines = stripFrontMatter(source).replace(/\r\n/g, '\n').split('\n');
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    const opener = line.match(/^\s*(`{3,}|~{3,})/);
    if (fence) {
      if (opener && opener[1][0] === fence[0] && opener[1].length >= fence.length) fence = null;
      out.push(line);
      continue;
    }
    if (opener) {
      fence = opener[1];
      out.push(line.replace(/^(\s*`{3,}\w*)\s+title=.*$/, '$1'));
      continue;
    }
    // A component may span several lines: collect up to its closing "/>".
    if (/^\s*<(Screenshot|Video)\b/.test(line)) {
      while (!/\/>\s*$/.test(line) && i + 1 < lines.length) line += ' ' + lines[++i].trim();
      const kind = line.match(/<(\w+)/)[1];
      const text = kind === 'Screenshot' ? attr(line, 'alt') : attr(line, 'title');
      const caption = attr(line, 'caption');
      out.push(`[${kind}: ${text ?? attr(line, 'name')}${caption ? ` (${caption})` : ''}]`);
      continue;
    }
    if (/^\s*import\s.+from\s/.test(line) || /^\s*export\s/.test(line)) continue;
    const admonition = line.match(/^:::(\w+)(?:\s+(.*))?$/);
    if (admonition) {
      const kind = admonition[1][0].toUpperCase() + admonition[1].slice(1);
      out.push(admonition[2] ? `**${kind}: ${admonition[2]}**` : `**${kind}:**`);
      continue;
    }
    if (/^:::\s*$/.test(line)) continue;
    // Cleaning, not sanitising: the input is the repository's own docs and llms-full.txt is
    // plain text, never rendered as HTML (src/autoair/docs/build.py ports the same rules).
    line = line
      .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/\\([{}<>])/g, '$1')
      .replace(/\]\((\/[^)\s]*)\)/g, (_, path) => `](${absolute(path)})`);
    out.push(line);
  }
  return out
    .join('\n')
    .replace(/^# .*\n+/m, '') // the page title is written by the caller
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** First sentence of a description, for the one-line entries of llms.txt. */
function oneLine(text) {
  const flat = (text ?? '').replace(/\s+/g, ' ').trim();
  const m = flat.match(/^(.+?[.!?])(\s|$)/);
  return m ? m[1] : flat;
}

/** Sidebar items flattened to doc ids, in order (a category's own page first). */
function docIds(items) {
  const ids = [];
  for (const item of items) {
    if (item.type === 'doc' || item.type === 'ref') ids.push(item.id);
    if (item.type === 'category') {
      if (item.link?.type === 'doc') ids.push(item.link.id);
      ids.push(...docIds(item.items));
    }
  }
  return ids;
}

export default function llmsTxtPlugin(context) {
  const {siteConfig, siteDir} = context;
  const origin = siteConfig.url.replace(/\/$/, '');
  const base = siteConfig.baseUrl;
  const absolute = (path) => (path.startsWith(base) ? `${origin}${path}` : `${origin}${base}${path.replace(/^\//, '')}`);
  let sections = [];

  return {
    name: 'autoair-llms-txt',

    async allContentLoaded({allContent}) {
      const version = allContent['docusaurus-plugin-content-docs'].default.loadedVersions[0];
      const docs = new Map(version.docs.map((d) => [d.id, d]));
      const sidebar = version.sidebars.docs;
      sections = sidebar.map((item) => {
        const ids = docIds([item]);
        const label = item.type === 'category' ? item.label : docs.get(item.id)?.title;
        return {label, docs: [...new Set(ids)].map((id) => docs.get(id)).filter(Boolean)};
      });
    },

    async postBuild({outDir}) {
      const index = [`# ${siteConfig.title}`, '', `> ${SUMMARY}`, '', INTRO, ''];
      const full = [`# ${siteConfig.title}`, '', `> ${SUMMARY}`, ''];
      for (const section of sections) {
        index.push(`## ${section.label}`, '');
        for (const doc of section.docs) {
          const description = oneLine(doc.description);
          index.push(`- [${doc.title}](${absolute(doc.permalink)})${description ? `: ${description}` : ''}`);
          const source = readFileSync(resolve(siteDir, doc.source.replace(/^@site\//, '')), 'utf8');
          full.push(`# ${doc.title}`, '', `Source: ${absolute(doc.permalink)}`, '', toMarkdown(source, absolute), '');
        }
        index.push('');
      }
      index.push(
        '## Optional',
        '',
        `- [Full documentation text](${absolute('/llms-full.txt')}): every page above as Markdown, in this order`,
        `- [Documentation MCP server](${absolute('/docs/ai/docs-mcp')}): search_autoair_docs and read_autoair_doc for coding agents (\`uvx --from 'autoair[mcp]' autoair docs-mcp\`)`,
        `- [REST API reference](${absolute('/api')}): the OpenAPI description of the AutoAir server, rendered`,
        `- [OpenAPI document](${absolute('/openapi.json')}): the same API as JSON`,
        `- [Release notes](${absolute('/blog')}): changes per release`,
        '',
      );
      writeFileSync(resolve(outDir, 'llms.txt'), index.join('\n'));
      writeFileSync(resolve(outDir, 'llms-full.txt'), full.join('\n'));
      const pages = sections.reduce((n, s) => n + s.docs.length, 0);
      console.log(`[llms-txt] wrote llms.txt and llms-full.txt (${pages} pages)`);
    },
  };
}
