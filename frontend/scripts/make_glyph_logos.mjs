// Generates the generic "concept" logos (robot for agents, clock for time sensors…) used for
// operators without a vendor logo: Lucide glyphs (ISC licence) on a coloured rounded tile.
// Run from frontend/:  node scripts/make_glyph_logos.mjs
import { writeFileSync } from "node:fs";
import { URL } from "node:url";
import console from "node:console";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as lu from "react-icons/lu";

const OUT = new URL("../../src/autoair/resources/logos/", import.meta.url);
export const GLYPHS = {
  agent: ["LuBot", "#7c3aed"],
  llm: ["LuSparkles", "#8b5cf6"],
  embedding: ["LuLayers", "#6366f1"],
  rerank: ["LuArrowDownUp", "#6366f1"],
  retrieval: ["LuSearch", "#6366f1"],
  document: ["LuFileText", "#0f766e"],
  "sql-ai": ["LuDatabaseZap", "#7c3aed"],
  "schema-compare": ["LuGitCompare", "#7c3aed"],
  human: ["LuUserCheck", "#059669"],
  branch: ["LuGitBranch", "#0284c7"],
  clock: ["LuClock", "#d97706"],
  calendar: ["LuCalendarDays", "#d97706"],
  hourglass: ["LuHourglass", "#d97706"],
  trigger: ["LuPlay", "#2563eb"],
  link: ["LuLink", "#475569"],
  empty: ["LuCircleDashed", "#64748b"],
  latest: ["LuSkipForward", "#475569"],
  smooth: ["LuWaves", "#0891b2"],
  transfer: ["LuArrowLeftRight", "#0f766e"],
  terminal: ["LuTerminal", "#1e3a8a"],
  sync: ["LuRefreshCw", "#0073ff"],
  quality: ["LuBadgeCheck", "#ff6310"],
};

for (const [name, [icon, color]] of Object.entries(GLYPHS)) {
  const Icon = lu[icon];
  if (!Icon) throw new Error(`unknown icon ${icon}`);
  const svg = renderToStaticMarkup(createElement(Icon));
  const inner = svg.replace(/^<svg[^>]*>/, "").replace(/<\/svg>$/, "");
  const tile =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">` +
    `<rect width="64" height="64" rx="14" fill="${color}"/>` +
    `<g transform="translate(14 14) scale(1.5)" fill="none" stroke="#fff" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round">${inner}</g></svg>\n`;
  writeFileSync(new URL(`glyph-${name}.svg`, OUT), tile);
}
console.log(`${Object.keys(GLYPHS).length} glyph logos written`);
