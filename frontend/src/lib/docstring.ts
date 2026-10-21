/**
 * Python docstrings → Markdown, so operator / decorator docs read like VS Code or PyCharm
 * hovers instead of raw reStructuredText. Handles the formats found in Airflow and providers:
 *
 * - Sphinx / reStructuredText: `:param x:`, `:type x:`, `:return:`, `:raises E:`, roles
 *   (`:ref:`, `:class:`…), ``literals``, directives (`.. seealso::`, `.. note::`,
 *   `.. code-block:: python`, `.. versionadded::`…), `::` literal blocks, section underlines,
 *   `` `text <url>`_ `` links;
 * - Google style (`Args:`, `Returns:`, `Raises:` …);
 * - NumPy style (`Parameters` + `----------`, `name : type`);
 * - Epytext (`@param x:`, `@return:`).
 *
 * Parameters end up in one "Parameters" list with their types, whatever the source format.
 */

type DocParam = { name: string; type?: string | undefined; description: string };

type ParsedDocstring = {
  /** Markdown body (description, notes, examples) without the parameter fields. */
  body: string;
  params: DocParam[];
  returns?: string | undefined;
  raises: { type: string; description: string }[];
};

/** `inspect.cleandoc`: strip the common indentation of all lines but the first. */
function cleandoc(doc: string): string {
  const lines = doc.replace(/\t/g, "    ").split("\n");
  const indents = lines
    .slice(1)
    .filter((l) => l.trim())
    .map((l) => l.length - l.trimStart().length);
  const margin = indents.length ? Math.min(...indents) : 0;
  const out = [lines[0]?.trim() ?? "", ...lines.slice(1).map((l) => l.slice(margin))];
  while (out.length && !out[0]?.trim()) out.shift();
  while (out.length && !out[out.length - 1]?.trim()) out.pop();
  return out.join("\n");
}

const indentOf = (line: string) => line.length - line.trimStart().length;

/** Inline reST → Markdown (roles, literals, links). */
export function inlineRst(text: string): string {
  return (
    text
      // `text <https://url>`_  /  `text <url>`__
      .replace(/`([^`<]+?)\s*<([^>]+)>`__?/g, (_m, label: string, url: string) => `[${label.trim()}](${url})`)
      // :ref:`label <target>` → label ; :ref:`howto/operator:PythonOperator` → PythonOperator
      .replace(/:(?:py:)?ref:`([^`<]+?)\s*<[^>]+>`/g, "_$1_")
      .replace(/:(?:py:)?(?:ref|doc):`([^`]+)`/g, (_m, target: string) => `_${target.split(/[:/]/).pop() ?? target}_`)
      // :class:`~airflow.models.BaseOperator` → `BaseOperator` (~ = short name), others full
      .replace(
        /:(?:py:)?(?:class|func|meth|mod|attr|obj|exc|data|const|paramref|envvar|term|program|option|command|file|kbd|abbr|any):`!?(~?)([^`]+)`/g,
        (_m, tilde: string, target: string) => {
          const clean = target.replace(/\s*<.*>$/, "").replace(/\(\)$/, "");
          return `\`${tilde ? (clean.split(".").pop() ?? clean) : clean}\``;
        },
      )
      // ``literal`` → `literal`
      .replace(/``([^`]+)``/g, "`$1`")
  );
}

const ADMONITIONS: Record<string, string> = {
  seealso: "See also",
  note: "Note",
  warning: "Warning",
  caution: "Caution",
  danger: "Danger",
  important: "Important",
  tip: "Tip",
  hint: "Hint",
  attention: "Attention",
  todo: "To do",
  versionadded: "New in version",
  versionchanged: "Changed in version",
  deprecated: "Deprecated since version",
};

const GOOGLE_SECTIONS: Record<string, "params" | "returns" | "raises" | "other"> = {
  args: "params",
  arguments: "params",
  parameters: "params",
  params: "params",
  "keyword args": "params",
  "keyword arguments": "params",
  kwargs: "params",
  "other parameters": "params",
  attributes: "params",
  returns: "returns",
  return: "returns",
  yields: "returns",
  yield: "returns",
  raises: "raises",
  raise: "raises",
  exceptions: "raises",
};

/** Lines indented deeper than `base` that follow `start` (a block body), blank lines included. */
function takeBlock(lines: string[], start: number, base: number): { body: string[]; next: number } {
  const body: string[] = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() && indentOf(line) <= base) break;
    body.push(line);
    i++;
  }
  while (body.length && !body[body.length - 1]?.trim()) body.pop();
  const margin = Math.min(...body.filter((l) => l.trim()).map(indentOf), Infinity);
  return { body: body.map((l) => (Number.isFinite(margin) ? l.slice(margin) : l)), next: i };
}

/** Field continuation: following lines indented deeper than the field line. */
function continuation(lines: string[], start: number, base: number): { text: string; next: number } {
  const { body, next } = takeBlock(lines, start, base);
  return {
    text: body
      .map((l) => l.trim())
      .join(" ")
      .replace(/\s+/g, " ")
      .trim(),
    next,
  };
}

export function parseDocstring(raw: string): ParsedDocstring {
  const lines = cleandoc(raw).split("\n");
  const out: string[] = [];
  const params = new Map<string, DocParam>();
  const raises: ParsedDocstring["raises"] = [];
  let returns: string | undefined;
  let returnType: string | undefined;
  const param = (name: string) => {
    const clean = name.trim();
    let p = params.get(clean);
    if (!p) {
      p = { name: clean, description: "" };
      params.set(clean, p);
    }
    return p;
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const trimmed = line.trim();
    const base = indentOf(line);

    // ---- Sphinx fields: :param [type] name: desc / :type name: t / :return: / :raises E:
    // `:param [type] name: description` — the type never contains a colon, so a colon inside
    // the description (`s3://`, `:ref:`…) cannot be mistaken for the end of the field name.
    let m = /^:(param|parameter|arg|argument|key|keyword)\s+(?:([^:]+?)\s+)??([*\w]+)\s*:(.*)$/.exec(trimmed);
    if (m) {
      const c = continuation(lines, i + 1, base);
      const p = param(m[3] ?? "");
      if (m[2]) p.type = m[2];
      p.description = inlineRst([m[4]?.trim(), c.text].filter(Boolean).join(" "));
      i = c.next;
      continue;
    }
    m = /^:type\s+([*\w]+)\s*:(.*)$/.exec(trimmed);
    if (m) {
      const c = continuation(lines, i + 1, base);
      param(m[1] ?? "").type = inlineRst([m[2]?.trim(), c.text].filter(Boolean).join(" ")).replace(/`/g, "");
      i = c.next;
      continue;
    }
    m = /^:(returns?|return)\s*:(.*)$/.exec(trimmed) ?? /^:rtype\s*:(.*)$/.exec(trimmed);
    if (m) {
      const c = continuation(lines, i + 1, base);
      const text = inlineRst([(m[2] ?? m[1])?.trim(), c.text].filter(Boolean).join(" "));
      // :return: and :rtype: may come in either order: keep both parts.
      if (trimmed.startsWith(":rtype")) returnType = text.replace(/`/g, "");
      else returns = text;
      i = c.next;
      continue;
    }
    m = /^:raises?\s+([\w.]+)\s*:(.*)$/.exec(trimmed);
    if (m) {
      const c = continuation(lines, i + 1, base);
      raises.push({ type: m[1] ?? "", description: inlineRst([m[2]?.trim(), c.text].filter(Boolean).join(" ")) });
      i = c.next;
      continue;
    }
    // ---- Epytext: @param name: desc / @type name: t / @return: / @raise E:
    m = /^@(param|type|return|returns|raise|raises|rtype)\s*([*\w.]*)\s*:(.*)$/.exec(trimmed);
    if (m) {
      const c = continuation(lines, i + 1, base);
      const text = inlineRst([m[3]?.trim(), c.text].filter(Boolean).join(" "));
      const tag = m[1] ?? "";
      if (tag === "param") param(m[2] ?? "").description = text;
      else if (tag === "type") param(m[2] ?? "").type = text;
      else if (tag.startsWith("raise")) raises.push({ type: m[2] ?? "", description: text });
      else returns = text;
      i = c.next;
      continue;
    }

    // ---- NumPy sections: "Parameters" + "----------"
    const underline = lines[i + 1]?.trim() ?? "";
    const numpyKind = GOOGLE_SECTIONS[trimmed.toLowerCase()];
    if (numpyKind && /^-{3,}$/.test(underline)) {
      i += 2;
      while (i < lines.length) {
        const entry = lines[i] ?? "";
        if (!entry.trim()) {
          i++;
          // A blank line followed by a new section title ends the section.
          if (/^-{3,}$/.test(lines[i + 1]?.trim() ?? "")) break;
          continue;
        }
        if (indentOf(entry) > base) break;
        if (/^-{3,}$/.test(lines[i + 1]?.trim() ?? "")) break;
        const [name, type] = entry.split(/\s+:\s+|\s*:\s*$/);
        const c = continuation(lines, i + 1, indentOf(entry));
        if (numpyKind === "params") {
          for (const n of (name ?? "").split(/,\s*/)) {
            const p = param(n.trim());
            if (type) p.type = type.trim();
            p.description = inlineRst(c.text);
          }
        } else if (numpyKind === "raises") raises.push({ type: (name ?? "").trim(), description: inlineRst(c.text) });
        else
          returns = inlineRst(
            [type ? `\`${type.trim()}\`` : `\`${(name ?? "").trim()}\``, c.text].filter(Boolean).join(" — "),
          );
        i = c.next;
      }
      continue;
    }

    // ---- Google sections: "Args:" with indented "name (type): description"
    const googleKind = GOOGLE_SECTIONS[trimmed.replace(/:$/, "").toLowerCase()];
    if (googleKind && trimmed.endsWith(":") && indentOf(lines[i + 1] ?? "") > base) {
      const { body, next } = takeBlock(lines, i + 1, base);
      let j = 0;
      while (j < body.length) {
        const entry = body[j] ?? "";
        if (!entry.trim()) {
          j++;
          continue;
        }
        const c = continuation(body, j + 1, indentOf(entry));
        const em = /^([*\w.]+)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$/.exec(entry.trim());
        const text = inlineRst([em ? em[3] : entry.trim(), c.text].filter(Boolean).join(" "));
        if (googleKind === "params" && em) {
          const p = param(em[1] ?? "");
          if (em[2]) p.type = em[2];
          p.description = text;
        } else if (googleKind === "raises" && em) raises.push({ type: em[1] ?? "", description: text });
        else if (googleKind === "returns") {
          // "bool: True on success." → type + description
          const typed = em && !em[2] ? `\`${em[1]}\` — ${inlineRst([em[3], c.text].filter(Boolean).join(" "))}` : text;
          returns = returns ? `${returns} ${typed}` : typed;
        } else out.push(text);
        j = c.next;
      }
      i = next;
      continue;
    }

    // ---- Directives: admonitions, code blocks
    m = /^\.\.\s+([\w-]+)::\s*(.*)$/.exec(trimmed);
    if (m) {
      const name = (m[1] ?? "").toLowerCase();
      const arg = m[2] ?? "";
      const { body, next } = takeBlock(lines, i + 1, base);
      // Directive options (":linenos:", ":caption: x") come first; roles (":ref:`x`") are content.
      let optionCount = 0;
      while (optionCount < body.length && /^:[\w-]+:(\s|$)/.test(body[optionCount]?.trim() ?? "")) optionCount++;
      const content = body.slice(optionCount);
      if (name === "code-block" || name === "code" || name === "sourcecode" || name === "testcode") {
        out.push(`\`\`\`${arg.trim() || "python"}`, ...dropBlank(content), "```");
      } else if (ADMONITIONS[name]) {
        const title = ADMONITIONS[name] ?? name;
        const head = arg ? `**${title}** ${inlineRst(arg)}` : `**${title}**`;
        const text = dropBlank(content).map((l) => inlineRst(l));
        out.push(`> ${head}`, ...(text.length ? [">", ...text.map((l) => (l ? `> ${l}` : ">"))] : []));
      } else if (name !== "autosummary" && name !== "toctree" && name !== "image" && name !== "include") {
        out.push(...dropBlank(content).map((l) => inlineRst(l)));
      }
      out.push("");
      i = next;
      continue;
    }

    // ---- Section titles with underlines (=== --- ~~~)
    if (trimmed && /^([=\-~^"'`#*+])\1{2,}$/.test(underline) && underline.length >= trimmed.length - 2) {
      out.push(`#### ${inlineRst(trimmed)}`, "");
      i += 2;
      continue;
    }

    // ---- Paragraph ending in "::" introduces a literal block.
    if (trimmed.endsWith("::") && !trimmed.startsWith("..")) {
      const intro = trimmed === "::" ? "" : trimmed.slice(0, -1);
      if (intro) out.push(inlineRst(intro));
      let k = i + 1;
      while (k < lines.length && !lines[k]?.trim()) k++;
      if (k < lines.length && indentOf(lines[k] ?? "") > base) {
        const { body, next } = takeBlock(lines, k, base);
        out.push("", "```python", ...dropBlank(body), "```", "");
        i = next;
        continue;
      }
      i++;
      continue;
    }

    // ---- Doctest (>>> …) blocks
    if (trimmed.startsWith(">>>")) {
      const block: string[] = [];
      while (i < lines.length && lines[i]?.trim()) {
        block.push((lines[i] ?? "").trim());
        i++;
      }
      out.push("```python", ...block, "```");
      continue;
    }

    out.push(inlineRst(line));
    i++;
  }

  if (returnType) returns = returns ? `${returns} (\`${returnType}\`)` : `\`${returnType}\``;
  return {
    body: collapseBlank(out).join("\n").trim(),
    params: [...params.values()],
    returns,
    raises,
  };
}

function dropBlank(lines: string[]): string[] {
  const out = [...lines];
  while (out.length && !out[0]?.trim()) out.shift();
  while (out.length && !out[out.length - 1]?.trim()) out.pop();
  return out;
}

function collapseBlank(lines: string[]): string[] {
  return lines.filter((l, k) => l.trim() || lines[k - 1]?.trim());
}

/** Full Markdown for a docstring: body, then Parameters / Returns / Raises sections. */
export function docstringToMarkdown(raw: string, opts: { params?: boolean } = {}): string {
  const doc = parseDocstring(raw);
  const parts = [doc.body];
  if (opts.params !== false && doc.params.length) {
    parts.push(
      "#### Parameters",
      doc.params
        .map(
          (p) =>
            `- **\`${p.name}\`**${p.type ? ` _(${p.type.replace(/`/g, "")})_` : ""}${p.description ? ` — ${p.description}` : ""}`,
        )
        .join("\n"),
    );
  }
  if (doc.returns) parts.push("#### Returns", doc.returns);
  if (doc.raises.length) {
    parts.push(
      "#### Raises",
      doc.raises.map((r) => `- \`${r.type}\`${r.description ? ` — ${r.description}` : ""}`).join("\n"),
    );
  }
  return parts.filter(Boolean).join("\n\n");
}
