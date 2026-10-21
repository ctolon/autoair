/**
 * Client-side Jinja preview for Airflow templated fields.
 *
 * A deliberately tiny, safe subset of Jinja: a tokenizer for text / `{{ expr }}` /
 * `{% stmt %}` / `{# comment #}`, an expression parser and a tree-walking evaluator over a
 * sample Airflow context. Nothing is ever passed to `eval`/`Function`; only whitelisted
 * functions, methods and filters run. Whatever cannot be known client-side (Variables,
 * connections, missing XComs, statements) renders as `⟨source⟩`.
 */

import { DAG_RUN_LOGICAL_DATE_ATTRS, LOGICAL_DATE_NAMES } from "./generated/jinjaContext.gen";

// ----------------------------------------------------------------------------- values

/** Python-ish datetime (UTC). `pendulum` mimics Airflow's context dates (ISO `str()`). */
class PyDateTime {
  constructor(
    readonly ms: number,
    readonly pendulum = true,
  ) {}
  get date(): Date {
    return new Date(this.ms);
  }
}

class PyTimedelta {
  constructor(readonly ms: number) {}
}

class PyFunction {
  constructor(
    readonly name: string,
    readonly fn: (args: Value[], kwargs: Record<string, Value>) => Value,
  ) {}
}

/** A value that cannot be known in the browser (Variables, connections, …). */
class Opaque {
  constructor(readonly path: string) {}
}

type Value =
  | null
  | boolean
  | number
  | string
  | Value[]
  | PyDateTime
  | PyTimedelta
  | PyFunction
  | Opaque
  | { [key: string]: Value };

type Dict = { [key: string]: Value };

const isDict = (v: Value): v is Dict =>
  typeof v === "object" &&
  v !== null &&
  !Array.isArray(v) &&
  !(v instanceof PyDateTime) &&
  !(v instanceof PyTimedelta) &&
  !(v instanceof PyFunction) &&
  !(v instanceof Opaque);

class EvalError extends Error {}

// ----------------------------------------------------------------------------- dates

const pad = (n: number, w = 2) => String(Math.abs(n)).padStart(w, "0");
const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Subset of Python's `strftime` (UTC). */
export function strftime(d: Date, fmt: string, pendulum = true): string {
  const tz = pendulum ? "+0000" : "";
  return fmt.replace(/%([a-zA-Z%])/g, (m, c: string) => {
    switch (c) {
      case "Y":
        return String(d.getUTCFullYear());
      case "y":
        return pad(d.getUTCFullYear() % 100);
      case "m":
        return pad(d.getUTCMonth() + 1);
      case "d":
        return pad(d.getUTCDate());
      case "e":
        return String(d.getUTCDate()).padStart(2, " ");
      case "H":
        return pad(d.getUTCHours());
      case "I":
        return pad(d.getUTCHours() % 12 || 12);
      case "p":
        return d.getUTCHours() < 12 ? "AM" : "PM";
      case "M":
        return pad(d.getUTCMinutes());
      case "S":
        return pad(d.getUTCSeconds());
      case "f":
        return pad(d.getUTCMilliseconds() * 1000, 6);
      case "b":
        return (MONTHS[d.getUTCMonth()] ?? "").slice(0, 3);
      case "B":
        return MONTHS[d.getUTCMonth()] ?? "";
      case "a":
        return (DAYS[d.getUTCDay()] ?? "").slice(0, 3);
      case "A":
        return DAYS[d.getUTCDay()] ?? "";
      case "w":
        return String(d.getUTCDay());
      case "j": {
        const start = Date.UTC(d.getUTCFullYear(), 0, 1);
        return pad(Math.floor((d.getTime() - start) / 86_400_000) + 1, 3);
      }
      case "z":
        return tz;
      case "Z":
        return pendulum ? "UTC" : "";
      case "%":
        return "%";
      default:
        return m;
    }
  });
}

/** Subset of Python's `strptime` (numeric fields + %b/%B). Returns null on mismatch. */
export function strptime(s: string, fmt: string): Date | null {
  let re = "";
  const fields: string[] = [];
  for (let i = 0; i < fmt.length; i++) {
    const c = fmt[i] as string;
    if (c === "%" && i + 1 < fmt.length) {
      const f = fmt[++i] as string;
      const pat: Record<string, string> = {
        Y: "(\\d{4})",
        y: "(\\d{2})",
        m: "(\\d{1,2})",
        d: "(\\d{1,2})",
        H: "(\\d{1,2})",
        M: "(\\d{1,2})",
        S: "(\\d{1,2})",
        b: "([A-Za-z]{3})",
        B: "([A-Za-z]+)",
        "%": "%",
      };
      const p = Object.hasOwn(pat, f) ? pat[f] : undefined;
      if (!p) return null;
      re += p;
      if (f !== "%") fields.push(f);
    } else re += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }
  const m = new RegExp(`^${re}$`).exec(s);
  if (!m) return null;
  const parts = { Y: 1900, m: 1, d: 1, H: 0, M: 0, S: 0 };
  fields.forEach((f, i) => {
    const v = m[i + 1] ?? "";
    if (f === "b" || f === "B") {
      const idx = MONTHS.findIndex((n) => n.toLowerCase().startsWith(v.toLowerCase().slice(0, 3)));
      parts.m = idx + 1;
    } else if (f === "y") parts.Y = 2000 + Number(v);
    else parts[f as keyof typeof parts] = Number(v);
  });
  if (parts.m < 1) return null;
  return new Date(Date.UTC(parts.Y, parts.m - 1, parts.d, parts.H, parts.M, parts.S));
}

function isoformat(dt: PyDateTime, sep = "T"): string {
  const d = dt.date;
  const ms = d.getUTCMilliseconds();
  const base = `${strftime(d, "%Y-%m-%d")}${sep}${strftime(d, "%H:%M:%S")}${ms ? `.${pad(ms * 1000, 6)}` : ""}`;
  return dt.pendulum ? `${base}+00:00` : base;
}

const dsOf = (d: Date) => strftime(d, "%Y-%m-%d");

// ----------------------------------------------------------------------------- tokenizer

type TemplatePart =
  | { kind: "text"; text: string }
  | { kind: "expr"; src: string; raw: string }
  | { kind: "stmt"; src: string; raw: string }
  | { kind: "comment"; raw: string };

/** Splits a template into literal text, expressions, statements and comments. */
export function splitTemplate(template: string): TemplatePart[] {
  const parts: TemplatePart[] = [];
  const re = /\{\{([\s\S]*?)\}\}|\{%([\s\S]*?)%\}|\{#[\s\S]*?#\}/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(template))) {
    if (m.index > last) parts.push({ kind: "text", text: template.slice(last, m.index) });
    const raw = m[0];
    const strip = (s: string) => s.replace(/^[-+]/, "").replace(/[-+]$/, "").trim();
    if (m[1] !== undefined) parts.push({ kind: "expr", src: strip(m[1]), raw });
    else if (m[2] !== undefined) parts.push({ kind: "stmt", src: strip(m[2]), raw });
    else parts.push({ kind: "comment", raw });
    last = m.index + raw.length;
  }
  if (last < template.length) parts.push({ kind: "text", text: template.slice(last) });
  return parts;
}

/** True when the string contains Jinja markup. */
export const isTemplateString = (s: string | null | undefined): boolean => !!s && /\{\{|\{%/.test(s);

type Tok =
  | { t: "num"; v: number; pos: number }
  | { t: "str"; v: string; pos: number }
  | { t: "name"; v: string; pos: number }
  | { t: "op"; v: string; pos: number };

const OPS = [
  "==",
  "!=",
  "<=",
  ">=",
  "//",
  "**",
  ".",
  "[",
  "]",
  "(",
  ")",
  "{",
  "}",
  ",",
  ":",
  "|",
  "~",
  "+",
  "-",
  "*",
  "/",
  "%",
  "<",
  ">",
  "=",
];

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i] as string;
    if (/\s/.test(c)) {
      i++;
      continue;
    }
    if (/[0-9]/.test(c)) {
      const m = /^\d+(\.\d+)?([eE][+-]?\d+)?/.exec(src.slice(i));
      const text = m?.[0] ?? c;
      out.push({ t: "num", v: Number(text), pos: i });
      i += text.length;
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const text = /^[A-Za-z_]\w*/.exec(src.slice(i))?.[0] ?? c;
      out.push({ t: "name", v: text, pos: i });
      i += text.length;
      continue;
    }
    if (c === '"' || c === "'") {
      let j = i + 1;
      let s = "";
      while (j < src.length && src[j] !== c) {
        if (src[j] === "\\" && j + 1 < src.length) {
          const n = src[j + 1] as string;
          s += ({ n: "\n", t: "\t", r: "\r", "\\": "\\", "'": "'", '"': '"' } as Record<string, string>)[n] ?? `\\${n}`;
          j += 2;
        } else s += src[j++];
      }
      if (j >= src.length) throw new EvalError("unterminated string");
      out.push({ t: "str", v: s, pos: i });
      i = j + 1;
      continue;
    }
    const op = OPS.find((o) => src.startsWith(o, i));
    if (!op) throw new EvalError(`unexpected character '${c}'`);
    out.push({ t: "op", v: op, pos: i });
    i += op.length;
  }
  return out;
}

// ----------------------------------------------------------------------------- parser

type Node =
  | { k: "lit"; v: Value }
  | { k: "name"; name: string }
  | { k: "attr"; obj: Node; name: string }
  | { k: "item"; obj: Node; index: Node }
  | { k: "call"; fn: Node; args: Node[]; kwargs: [string, Node][] }
  | { k: "filter"; value: Node; name: string; args: Node[]; kwargs: [string, Node][] }
  | { k: "bin"; op: string; a: Node; b: Node }
  | { k: "not"; a: Node }
  | { k: "neg"; a: Node }
  | { k: "cond"; test: Node; a: Node; b: Node | null }
  | { k: "list"; items: Node[] }
  | { k: "dict"; items: [Node, Node][] };

class Parser {
  private i = 0;
  constructor(private readonly toks: Tok[]) {}

  private peek(): Tok | undefined {
    return this.toks[this.i];
  }
  private isOp(v: string): boolean {
    const t = this.peek();
    return t?.t === "op" && t.v === v;
  }
  private isName(v: string): boolean {
    const t = this.peek();
    return t?.t === "name" && t.v === v;
  }
  private expectOp(v: string): void {
    if (!this.isOp(v)) throw new EvalError(`expected '${v}'`);
    this.i++;
  }

  parseAll(): Node {
    const n = this.expr();
    if (this.i < this.toks.length) {
      const t = this.peek();
      throw new EvalError(`unexpected '${t ? String(t.v) : ""}'`);
    }
    return n;
  }

  expr(): Node {
    const a = this.or();
    if (this.isName("if")) {
      this.i++;
      const test = this.or();
      let b: Node | null = null;
      if (this.isName("else")) {
        this.i++;
        b = this.expr();
      }
      return { k: "cond", test, a, b };
    }
    return a;
  }
  private or(): Node {
    let a = this.and();
    while (this.isName("or")) {
      this.i++;
      a = { k: "bin", op: "or", a, b: this.and() };
    }
    return a;
  }
  private and(): Node {
    let a = this.not();
    while (this.isName("and")) {
      this.i++;
      a = { k: "bin", op: "and", a, b: this.not() };
    }
    return a;
  }
  private not(): Node {
    if (this.isName("not")) {
      this.i++;
      return { k: "not", a: this.not() };
    }
    return this.compare();
  }
  private compare(): Node {
    let a = this.concat();
    for (;;) {
      const t = this.peek();
      if (t?.t === "op" && ["==", "!=", "<", ">", "<=", ">="].includes(t.v)) {
        this.i++;
        a = { k: "bin", op: t.v, a, b: this.concat() };
      } else if (this.isName("in")) {
        this.i++;
        a = { k: "bin", op: "in", a, b: this.concat() };
      } else if (this.isName("not") && this.toks[this.i + 1]?.t === "name" && this.toks[this.i + 1]?.v === "in") {
        this.i += 2;
        a = { k: "not", a: { k: "bin", op: "in", a, b: this.concat() } };
      } else return a;
    }
  }
  private concat(): Node {
    let a = this.additive();
    while (this.isOp("~")) {
      this.i++;
      a = { k: "bin", op: "~", a, b: this.additive() };
    }
    return a;
  }
  private additive(): Node {
    let a = this.mult();
    while (this.isOp("+") || this.isOp("-")) {
      const op = (this.peek() as Tok).v as string;
      this.i++;
      a = { k: "bin", op, a, b: this.mult() };
    }
    return a;
  }
  private mult(): Node {
    let a = this.unary();
    while (this.isOp("*") || this.isOp("/") || this.isOp("%") || this.isOp("//")) {
      const op = (this.peek() as Tok).v as string;
      this.i++;
      a = { k: "bin", op, a, b: this.unary() };
    }
    return a;
  }
  private unary(): Node {
    if (this.isOp("-")) {
      this.i++;
      return { k: "neg", a: this.unary() };
    }
    if (this.isOp("+")) {
      this.i++;
      return this.unary();
    }
    return this.filtered();
  }
  private filtered(): Node {
    let v = this.postfix();
    while (this.isOp("|")) {
      this.i++;
      const t = this.peek();
      if (t?.t !== "name") throw new EvalError("expected filter name");
      this.i++;
      let args: Node[] = [];
      let kwargs: [string, Node][] = [];
      if (this.isOp("(")) ({ args, kwargs } = this.callArgs());
      v = { k: "filter", value: v, name: t.v, args, kwargs };
    }
    return v;
  }
  private callArgs(): { args: Node[]; kwargs: [string, Node][] } {
    this.expectOp("(");
    const args: Node[] = [];
    const kwargs: [string, Node][] = [];
    while (!this.isOp(")")) {
      const t = this.peek();
      const next = this.toks[this.i + 1];
      if (t?.t === "name" && next?.t === "op" && next.v === "=") {
        this.i += 2;
        kwargs.push([t.v, this.expr()]);
      } else args.push(this.expr());
      if (!this.isOp(",")) break;
      this.i++;
    }
    this.expectOp(")");
    return { args, kwargs };
  }
  private postfix(): Node {
    let n = this.primary();
    for (;;) {
      if (this.isOp(".")) {
        this.i++;
        const t = this.peek();
        if (t?.t === "name") n = { k: "attr", obj: n, name: t.v };
        else if (t?.t === "num") n = { k: "item", obj: n, index: { k: "lit", v: t.v } };
        else throw new EvalError("expected attribute name");
        this.i++;
      } else if (this.isOp("[")) {
        this.i++;
        const index = this.expr();
        this.expectOp("]");
        n = { k: "item", obj: n, index };
      } else if (this.isOp("(")) {
        const { args, kwargs } = this.callArgs();
        n = { k: "call", fn: n, args, kwargs };
      } else return n;
    }
  }
  private primary(): Node {
    const t = this.peek();
    if (!t) throw new EvalError("unexpected end of expression");
    this.i++;
    if (t.t === "num") return { k: "lit", v: t.v };
    if (t.t === "str") {
      let s = t.v;
      while (this.peek()?.t === "str") s += (this.toks[this.i++] as { v: string }).v;
      return { k: "lit", v: s };
    }
    if (t.t === "name") {
      if (t.v === "True" || t.v === "true") return { k: "lit", v: true };
      if (t.v === "False" || t.v === "false") return { k: "lit", v: false };
      if (t.v === "None" || t.v === "none") return { k: "lit", v: null };
      return { k: "name", name: t.v };
    }
    if (t.v === "(") {
      const e = this.expr();
      if (this.isOp(",")) {
        // tuple → list
        const items = [e];
        while (this.isOp(",")) {
          this.i++;
          if (this.isOp(")")) break;
          items.push(this.expr());
        }
        this.expectOp(")");
        return { k: "list", items };
      }
      this.expectOp(")");
      return e;
    }
    if (t.v === "[") {
      const items: Node[] = [];
      while (!this.isOp("]")) {
        items.push(this.expr());
        if (!this.isOp(",")) break;
        this.i++;
      }
      this.expectOp("]");
      return { k: "list", items };
    }
    if (t.v === "{") {
      const items: [Node, Node][] = [];
      while (!this.isOp("}")) {
        const key = this.expr();
        this.expectOp(":");
        items.push([key, this.expr()]);
        if (!this.isOp(",")) break;
        this.i++;
      }
      this.expectOp("}");
      return { k: "dict", items };
    }
    throw new EvalError(`unexpected '${t.v}'`);
  }
}

export function parseExpression(src: string): Node {
  return new Parser(tokenize(src)).parseAll();
}

// ----------------------------------------------------------------------------- python semantics

const pyRepr = (v: Value): string =>
  typeof v === "string" ? `'${v.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'` : pyStr(v);

/** Python `str()` of a value. */
function pyStr(v: Value): string {
  if (v === null) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : v > 0 ? "inf" : v < 0 ? "-inf" : "nan";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return `[${v.map(pyRepr).join(", ")}]`;
  if (v instanceof PyDateTime) return isoformat(v, v.pendulum ? "T" : " ");
  if (v instanceof PyTimedelta) {
    const total = Math.round(v.ms / 1000);
    const days = Math.floor(total / 86400);
    const rest = total - days * 86400;
    const hms = `${Math.floor(rest / 3600)}:${pad(Math.floor((rest % 3600) / 60))}:${pad(rest % 60)}`;
    return days ? `${days} day${Math.abs(days) === 1 ? "" : "s"}, ${hms}` : hms;
  }
  if (v instanceof PyFunction) return `<function ${v.name}>`;
  if (v instanceof Opaque) return `⟨${v.path}⟩`;
  return `{${Object.entries(v)
    .map(([k, x]) => `${pyRepr(k)}: ${pyRepr(x)}`)
    .join(", ")}}`;
}

const truthy = (v: Value): boolean => {
  if (v === null || v === false || v === 0 || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (isDict(v)) return Object.keys(v).length > 0;
  return true;
};

const toJson = (v: Value): unknown => {
  if (v instanceof PyDateTime) return pyStr(v);
  if (v instanceof PyTimedelta) return v.ms / 1000;
  if (Array.isArray(v)) return v.map(toJson);
  if (isDict(v)) return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, toJson(x)]));
  if (v instanceof PyFunction || v instanceof Opaque) return pyStr(v);
  return v;
};

const fromJson = (v: unknown): Value => {
  if (v === null || v === undefined) return null;
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return v;
  if (Array.isArray(v)) return v.map(fromJson);
  if (typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fromJson(x)]));
  return String(v);
};

const fn = (name: string, f: (args: Value[], kwargs: Record<string, Value>) => Value) => new PyFunction(name, f);

const num = (v: Value | undefined, what: string): number => {
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return v ? 1 : 0;
  throw new EvalError(`${what}: expected a number`);
};
const str = (v: Value | undefined, what: string): string => {
  if (typeof v === "string") return v;
  throw new EvalError(`${what}: expected a string`);
};

function timedeltaOf(kwargs: Record<string, Value>, args: Value[] = []): PyTimedelta {
  const unit: Record<string, number> = {
    weeks: 604_800_000,
    days: 86_400_000,
    hours: 3_600_000,
    minutes: 60_000,
    seconds: 1000,
    milliseconds: 1,
    microseconds: 0.001,
  };
  let ms = num(args[0] ?? 0, "timedelta") * 86_400_000 + num(args[1] ?? 0, "timedelta") * 1000;
  for (const [k, v] of Object.entries(kwargs)) {
    const u = Object.hasOwn(unit, k) ? unit[k] : undefined;
    if (u === undefined) throw new EvalError(`timedelta: unknown argument '${k}'`);
    ms += num(v, "timedelta") * u;
  }
  return new PyTimedelta(ms);
}

function dateMember(d: PyDateTime, name: string): Value {
  const date = d.date;
  switch (name) {
    case "year":
      return date.getUTCFullYear();
    case "month":
      return date.getUTCMonth() + 1;
    case "day":
      return date.getUTCDate();
    case "hour":
      return date.getUTCHours();
    case "minute":
      return date.getUTCMinutes();
    case "second":
      return date.getUTCSeconds();
    case "microsecond":
      return date.getUTCMilliseconds() * 1000;
    case "tzinfo":
    case "timezone":
    case "tz":
      return d.pendulum ? "UTC" : null;
    case "strftime":
      return fn("strftime", ([f]) => strftime(date, str(f, "strftime"), d.pendulum));
    case "isoformat":
      return fn("isoformat", ([sep]) => isoformat(d, typeof sep === "string" ? sep : "T"));
    case "date":
      return fn("date", () => dsOf(date));
    case "timestamp":
      return fn("timestamp", () => d.ms / 1000);
    case "weekday":
      return fn("weekday", () => (date.getUTCDay() + 6) % 7);
    case "isoweekday":
      return fn("isoweekday", () => date.getUTCDay() || 7);
    case "to_date_string":
      return fn("to_date_string", () => dsOf(date));
    case "to_datetime_string":
      return fn("to_datetime_string", () => `${dsOf(date)} ${strftime(date, "%H:%M:%S")}`);
    case "add":
    case "subtract":
    case "replace": {
      const sign = name === "subtract" ? -1 : 1;
      return fn(name, (_args, kwargs) => {
        if (name === "replace") {
          const r = new Date(d.ms);
          const set: Record<string, (n: number) => void> = {
            year: (n) => r.setUTCFullYear(n),
            month: (n) => r.setUTCMonth(n - 1),
            day: (n) => r.setUTCDate(n),
            hour: (n) => r.setUTCHours(n),
            minute: (n) => r.setUTCMinutes(n),
            second: (n) => r.setUTCSeconds(n),
            microsecond: (n) => r.setUTCMilliseconds(Math.floor(n / 1000)),
          };
          for (const [k, v] of Object.entries(kwargs)) if (Object.hasOwn(set, k)) set[k]?.(num(v, "replace"));
          return new PyDateTime(r.getTime(), d.pendulum);
        }
        const r = new Date(d.ms);
        const { years, months, ...rest } = kwargs;
        if (years !== undefined) r.setUTCFullYear(r.getUTCFullYear() + sign * num(years, name));
        if (months !== undefined) r.setUTCMonth(r.getUTCMonth() + sign * num(months, name));
        return new PyDateTime(r.getTime() + sign * timedeltaOf(rest).ms, d.pendulum);
      });
    }
    case "start_of":
    case "end_of":
      return fn(name, ([unitV]) => {
        const unit = str(unitV, name);
        const r = new Date(d.ms);
        const start = name === "start_of";
        if (unit === "day") {
          if (start) r.setUTCHours(0, 0, 0, 0);
          else r.setUTCHours(23, 59, 59, 999);
        } else if (unit === "month") {
          if (start) r.setUTCDate(1);
          else r.setUTCMonth(r.getUTCMonth() + 1, 0);
          r.setUTCHours(start ? 0 : 23, start ? 0 : 59, start ? 0 : 59, start ? 0 : 999);
        } else if (unit === "year") {
          if (start) r.setUTCMonth(0, 1);
          else r.setUTCMonth(11, 31);
          r.setUTCHours(start ? 0 : 23, start ? 0 : 59, start ? 0 : 59, start ? 0 : 999);
        } else if (unit === "hour") {
          if (start) r.setUTCMinutes(0, 0, 0);
          else r.setUTCMinutes(59, 59, 999);
        } else throw new EvalError(`${name}: unsupported unit '${unit}'`);
        return new PyDateTime(r.getTime(), d.pendulum);
      });
    default:
      throw new EvalError(`datetime has no attribute '${name}'`);
  }
}

function strMember(s: string, name: string): Value {
  const methods: Record<string, (args: Value[]) => Value> = {
    upper: () => s.toUpperCase(),
    lower: () => s.toLowerCase(),
    strip: ([c]) => (typeof c === "string" ? trimChars(s, c) : s.trim()),
    lstrip: () => s.trimStart(),
    rstrip: () => s.trimEnd(),
    title: () => titleCase(s),
    capitalize: () => capitalize(s),
    replace: ([a, b]) => s.split(str(a, "replace")).join(str(b, "replace")),
    split: ([sep]) => (typeof sep === "string" ? s.split(sep) : s.trim().split(/\s+/).filter(Boolean)),
    startswith: ([p]) => s.startsWith(str(p, "startswith")),
    endswith: ([p]) => s.endsWith(str(p, "endswith")),
    zfill: ([w]) => s.padStart(num(w, "zfill"), "0"),
    join: ([items]) => (Array.isArray(items) ? items.map(pyStr).join(s) : str(items, "join")),
    format: (args) => {
      let i = 0;
      return s.replace(/\{(\d*)\}/g, (_m, idx: string) => pyStr(args[idx === "" ? i++ : Number(idx)] ?? ""));
    },
  };
  const m = Object.hasOwn(methods, name) ? methods[name] : undefined;
  if (!m) throw new EvalError(`str has no attribute '${name}'`);
  return fn(name, (args) => m(args));
}

const trimChars = (s: string, chars: string) => {
  let a = 0;
  let b = s.length;
  while (a < b && chars.includes(s[a] as string)) a++;
  while (b > a && chars.includes(s[b - 1] as string)) b--;
  return s.slice(a, b);
};
const titleCase = (s: string) =>
  s.toLowerCase().replace(/(^|[^A-Za-z])([a-z])/g, (_m, p: string, c: string) => p + c.toUpperCase());
const capitalize = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : s);

function dictMember(d: Dict, name: string, path: string): Value {
  if (Object.hasOwn(d, name)) return d[name] as Value;
  switch (name) {
    case "get":
      return fn("get", ([k, dflt]) => {
        const key = pyStr(k ?? null);
        return Object.hasOwn(d, key) ? (d[key] as Value) : (dflt ?? null);
      });
    case "keys":
      return fn("keys", () => Object.keys(d));
    case "values":
      return fn("values", () => Object.values(d));
    case "items":
      return fn("items", () => Object.entries(d).map(([k, v]) => [k, v]));
    default:
      // Jinja would render Undefined (empty); unknown here means "not knowable in the browser".
      return new Opaque(path);
  }
}

// ----------------------------------------------------------------------------- filters

function datetimeArg(v: Value, f: string): PyDateTime {
  if (v instanceof PyDateTime) return v;
  if (typeof v === "string") {
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) return new PyDateTime(d.getTime());
  }
  throw new EvalError(`${f}: expected a datetime`);
}

const FILTERS: Record<string, (v: Value, args: Value[], kwargs: Record<string, Value>) => Value> = {
  upper: (v) => pyStr(v).toUpperCase(),
  lower: (v) => pyStr(v).toLowerCase(),
  title: (v) => titleCase(pyStr(v)),
  capitalize: (v) => capitalize(pyStr(v)),
  trim: (v, [chars]) => (typeof chars === "string" ? trimChars(pyStr(v), chars) : pyStr(v).trim()),
  string: (v) => pyStr(v),
  int: (v, [dflt]) => {
    const n = typeof v === "number" ? v : Number.parseFloat(pyStr(v));
    return Number.isFinite(n) ? Math.trunc(n) : typeof dflt === "number" ? dflt : 0;
  },
  float: (v, [dflt]) => {
    const n = typeof v === "number" ? v : Number.parseFloat(pyStr(v));
    return Number.isFinite(n) ? n : typeof dflt === "number" ? dflt : 0;
  },
  abs: (v) => Math.abs(num(v, "abs")),
  round: (v, [precision]) => {
    const p = typeof precision === "number" ? precision : 0;
    return Math.round(num(v, "round") * 10 ** p) / 10 ** p;
  },
  default: (v, [dflt, boolean]) => (v === null || (boolean === true && !truthy(v)) ? (dflt ?? "") : v),
  replace: (v, [a, b]) => pyStr(v).split(str(a, "replace")).join(str(b, "replace")),
  length: (v) => (Array.isArray(v) ? v.length : isDict(v) ? Object.keys(v).length : pyStr(v).length),
  join: (v, [sep]) => (Array.isArray(v) ? v.map(pyStr).join(typeof sep === "string" ? sep : "") : pyStr(v)),
  list: (v) => (Array.isArray(v) ? v : typeof v === "string" ? [...v] : isDict(v) ? Object.keys(v) : [v]),
  first: (v) => (Array.isArray(v) ? (v[0] ?? null) : pyStr(v).charAt(0)),
  last: (v) => (Array.isArray(v) ? (v[v.length - 1] ?? null) : pyStr(v).slice(-1)),
  sort: (v) => (Array.isArray(v) ? [...v].sort((a, b) => (pyStr(a) < pyStr(b) ? -1 : 1)) : v),
  unique: (v) => (Array.isArray(v) ? [...new Map(v.map((x) => [pyStr(x), x])).values()] : v),
  tojson: (v, [indent]) => JSON.stringify(toJson(v), null, typeof indent === "number" ? indent : undefined),
  quote: (v) => `'${pyStr(v)}'`,
  // Airflow template filters (airflow.templates.FILTERS)
  ds: (v) => strftime(datetimeArg(v, "ds").date, "%Y-%m-%d"),
  ds_nodash: (v) => strftime(datetimeArg(v, "ds_nodash").date, "%Y%m%d"),
  ts: (v) => isoformat(datetimeArg(v, "ts")),
  ts_nodash: (v) => strftime(datetimeArg(v, "ts_nodash").date, "%Y%m%dT%H%M%S"),
  ts_nodash_with_tz: (v) => isoformat(datetimeArg(v, "ts_nodash_with_tz")).replace(/[-:]/g, ""),
};

/** Names of the supported filters (used by completion). */
export const FILTER_NAMES = Object.keys(FILTERS);

// ----------------------------------------------------------------------------- evaluator

type Scope = Record<string, Value>;

/** Thrown to abort an expression that depends on something unknowable. */
class UnknownValue extends Error {
  constructor(readonly opaque: Opaque) {
    super(opaque.path);
  }
}

function srcOf(n: Node): string {
  switch (n.k) {
    case "name":
      return n.name;
    case "attr":
      return `${srcOf(n.obj)}.${n.name}`;
    case "item":
      return `${srcOf(n.obj)}[${n.index.k === "lit" ? pyRepr(n.index.v) : "…"}]`;
    case "call":
      return `${srcOf(n.fn)}(…)`;
    default:
      return "…";
  }
}

function evaluate(n: Node, scope: Scope): Value {
  switch (n.k) {
    case "lit":
      return n.v;
    case "name":
      return Object.hasOwn(scope, n.name) ? (scope[n.name] as Value) : new Opaque(n.name);
    case "attr": {
      const obj = evaluate(n.obj, scope);
      const path = `${srcOf(n.obj)}.${n.name}`;
      if (obj instanceof Opaque) return new Opaque(path);
      if (obj instanceof PyDateTime) return dateMember(obj, n.name);
      if (typeof obj === "string") return strMember(obj, n.name);
      if (obj instanceof PyTimedelta) {
        if (n.name === "days") return Math.floor(obj.ms / 86_400_000);
        if (n.name === "seconds") return Math.floor((obj.ms % 86_400_000) / 1000);
        if (n.name === "total_seconds") return fn("total_seconds", () => obj.ms / 1000);
        throw new EvalError(`timedelta has no attribute '${n.name}'`);
      }
      if (obj !== null && isDict(obj)) return dictMember(obj, n.name, path);
      throw new EvalError(`'${pyStr(obj)}' has no attribute '${n.name}'`);
    }
    case "item": {
      const obj = evaluate(n.obj, scope);
      const idx = evaluate(n.index, scope);
      if (obj instanceof Opaque) return new Opaque(srcOf(n));
      if (idx instanceof Opaque) throw new UnknownValue(idx);
      if (Array.isArray(obj) || typeof obj === "string") {
        const i = num(idx, "index");
        const j = i < 0 ? obj.length + i : i;
        const v = Array.isArray(obj) ? obj[j] : obj[j];
        if (v === undefined) throw new EvalError("index out of range");
        return v;
      }
      if (obj !== null && isDict(obj)) {
        const key = pyStr(idx);
        return Object.hasOwn(obj, key) ? (obj[key] as Value) : new Opaque(srcOf(n));
      }
      throw new EvalError("value is not subscriptable");
    }
    case "call": {
      const f = evaluate(n.fn, scope);
      if (f instanceof Opaque) return new Opaque(`${f.path}(…)`);
      if (!(f instanceof PyFunction)) throw new EvalError(`'${srcOf(n.fn)}' is not callable`);
      const args = n.args.map((a) => evaluate(a, scope));
      const kwargs = Object.fromEntries(n.kwargs.map(([k, v]) => [k, evaluate(v, scope)]));
      for (const v of [...args, ...Object.values(kwargs)])
        if (v instanceof Opaque) return new Opaque(`${srcOf(n.fn)}(…)`);
      return f.fn(args, kwargs);
    }
    case "filter": {
      const v = evaluate(n.value, scope);
      const f = Object.hasOwn(FILTERS, n.name) ? FILTERS[n.name] : undefined;
      if (!f) throw new EvalError(`unknown filter '${n.name}'`);
      const args = n.args.map((a) => evaluate(a, scope));
      const kwargs = Object.fromEntries(n.kwargs.map(([k, x]) => [k, evaluate(x, scope)]));
      if (v instanceof Opaque) return v;
      for (const a of args) if (a instanceof Opaque) return a;
      return f(v, args, kwargs);
    }
    case "not": {
      const a = evaluate(n.a, scope);
      if (a instanceof Opaque) return a;
      return !truthy(a);
    }
    case "neg": {
      const a = evaluate(n.a, scope);
      if (a instanceof Opaque) return a;
      if (a instanceof PyTimedelta) return new PyTimedelta(-a.ms);
      return -num(a, "-");
    }
    case "cond": {
      const test = evaluate(n.test, scope);
      if (test instanceof Opaque) return test;
      return truthy(test) ? evaluate(n.a, scope) : n.b ? evaluate(n.b, scope) : "";
    }
    case "list":
      return n.items.map((i) => evaluate(i, scope));
    case "dict":
      return Object.fromEntries(n.items.map(([k, v]) => [pyStr(evaluate(k, scope)), evaluate(v, scope)]));
    case "bin":
      return binary(n.op, n.a, n.b, scope);
  }
}

function binary(op: string, an: Node, bn: Node, scope: Scope): Value {
  const a = evaluate(an, scope);
  if (op === "and" || op === "or") {
    if (a instanceof Opaque) return a;
    if (op === "and") return truthy(a) ? evaluate(bn, scope) : a;
    return truthy(a) ? a : evaluate(bn, scope);
  }
  const b = evaluate(bn, scope);
  if (a instanceof Opaque) return a;
  if (b instanceof Opaque) return b;
  switch (op) {
    case "~":
      return pyStr(a) + pyStr(b);
    case "+":
      if (typeof a === "string" && typeof b === "string") return a + b;
      if (Array.isArray(a) && Array.isArray(b)) return [...a, ...b];
      if (a instanceof PyDateTime && b instanceof PyTimedelta) return new PyDateTime(a.ms + b.ms, a.pendulum);
      if (a instanceof PyTimedelta && b instanceof PyDateTime) return new PyDateTime(a.ms + b.ms, b.pendulum);
      if (a instanceof PyTimedelta && b instanceof PyTimedelta) return new PyTimedelta(a.ms + b.ms);
      return num(a, "+") + num(b, "+");
    case "-":
      if (a instanceof PyDateTime && b instanceof PyTimedelta) return new PyDateTime(a.ms - b.ms, a.pendulum);
      if (a instanceof PyDateTime && b instanceof PyDateTime) return new PyTimedelta(a.ms - b.ms);
      if (a instanceof PyTimedelta && b instanceof PyTimedelta) return new PyTimedelta(a.ms - b.ms);
      return num(a, "-") - num(b, "-");
    case "*":
      if (typeof a === "string" && typeof b === "number") return a.repeat(Math.max(0, b));
      return num(a, "*") * num(b, "*");
    case "/": {
      const d = num(b, "/");
      if (d === 0) throw new EvalError("division by zero");
      return num(a, "/") / d;
    }
    case "//": {
      const d = num(b, "//");
      if (d === 0) throw new EvalError("division by zero");
      return Math.floor(num(a, "//") / d);
    }
    case "%":
      if (typeof a === "string") return a.replace(/%[sd]/, pyStr(b));
      return num(a, "%") % num(b, "%");
    case "==":
      return pyStr(a) === pyStr(b) && typeof a === typeof b;
    case "!=":
      return !(pyStr(a) === pyStr(b) && typeof a === typeof b);
    case "<":
    case ">":
    case "<=":
    case ">=": {
      const x = typeof a === "number" ? a : a instanceof PyDateTime ? a.ms : pyStr(a);
      const y = typeof b === "number" ? b : b instanceof PyDateTime ? b.ms : pyStr(b);
      return op === "<" ? x < y : op === ">" ? x > y : op === "<=" ? x <= y : x >= y;
    }
    case "in":
      if (typeof b === "string") return b.includes(pyStr(a));
      if (Array.isArray(b)) return b.some((x) => pyStr(x) === pyStr(a));
      if (isDict(b)) return Object.hasOwn(b, pyStr(a));
      throw new EvalError("'in' needs a container");
    default:
      throw new EvalError(`unsupported operator '${op}'`);
  }
}

// ----------------------------------------------------------------------------- context

type SampleContextInput = {
  /** Logical date of the sample run (interpreted as UTC). */
  logicalDate: Date;
  dagId?: string | undefined;
  taskId?: string | undefined;
  /** DAG params (declared defaults). */
  params?: Record<string, unknown> | undefined;
  /** dag_run.conf of the sample run. */
  conf?: Record<string, unknown> | undefined;
  runType?: "scheduled" | "manual" | undefined;
  /** Data interval; defaults to [logical date, logical date] (trigger timetables). */
  dataIntervalStart?: Date | undefined;
  dataIntervalEnd?: Date | undefined;
  /** Known XCom values: task id → key → value ("return_value" for the returned value). */
  xcom?: Record<string, Record<string, unknown>> | undefined;
};

type SampleContext = Record<string, Value>;

/** Builds the Airflow template context used by the preview. */
export function sampleContext(input: SampleContextInput): SampleContext {
  const ms = input.logicalDate.getTime();
  const logical = new PyDateTime(ms);
  const d = input.logicalDate;
  const ts = isoformat(logical);
  const runId = `${input.runType ?? "scheduled"}__${ts}`;
  const dagId = input.dagId ?? "my_dag";
  const taskId = input.taskId ?? "task";
  const params = fromJson(input.params ?? {}) as Dict;
  const conf = fromJson(input.conf ?? {}) as Dict;
  const xcom = input.xcom ?? {};
  const start = new PyDateTime(input.dataIntervalStart?.getTime() ?? ms);
  const end = new PyDateTime(input.dataIntervalEnd?.getTime() ?? ms);

  const pullOne = (task: string, key: string): Value => {
    const byKey = xcom[task];
    if (!byKey || !Object.hasOwn(byKey, key))
      return new Opaque(`ti.xcom_pull(task_ids='${task}'${key === "return_value" ? "" : `, key='${key}'`})`);
    return fromJson(byKey[key]);
  };
  const xcomPull = fn("xcom_pull", (args, kwargs) => {
    const tasks = kwargs.task_ids ?? args[0] ?? null;
    const keyV = kwargs.key ?? args[2] ?? "return_value";
    const key = typeof keyV === "string" ? keyV : "return_value";
    if (tasks === null) return new Opaque("ti.xcom_pull(…)");
    if (Array.isArray(tasks)) {
      const values = tasks.map((t) => pullOne(pyStr(t), key));
      const unknown = values.find((v) => v instanceof Opaque);
      return unknown ?? values;
    }
    return pullOne(pyStr(tasks), key);
  });
  const ti: Dict = {
    task_id: taskId,
    dag_id: dagId,
    run_id: runId,
    try_number: 1,
    map_index: -1,
    xcom_pull: xcomPull,
  };
  const macros: Dict = {
    ds_add: fn("ds_add", ([ds, days]) => {
      const base = strptime(str(ds, "ds_add"), "%Y-%m-%d");
      if (!base) throw new EvalError("ds_add: expected YYYY-MM-DD");
      return dsOf(new Date(base.getTime() + num(days, "ds_add") * 86_400_000));
    }),
    ds_format: fn("ds_format", ([ds, input, output]) => {
      const parsed = strptime(str(ds, "ds_format"), str(input, "ds_format"));
      if (!parsed) throw new EvalError("ds_format: value does not match input format");
      return strftime(parsed, str(output, "ds_format"), false);
    }),
    datetime: fn("datetime", (args, kwargs) => {
      const [y, mo = 1, day = 1, h = 0, mi = 0, s = 0] = args.map((a) => num(a, "datetime"));
      const get = (k: string, dflt: number) => (kwargs[k] !== undefined ? num(kwargs[k], "datetime") : dflt);
      if (y === undefined && kwargs.year === undefined) throw new EvalError("datetime: year is required");
      return new PyDateTime(
        Date.UTC(
          get("year", y ?? 1970),
          get("month", mo) - 1,
          get("day", day),
          get("hour", h),
          get("minute", mi),
          get("second", s),
        ),
        false,
      );
    }),
    timedelta: fn("timedelta", (args, kwargs) => timedeltaOf(kwargs, args)),
    time: new Opaque("macros.time"),
    uuid: new Opaque("macros.uuid"),
    random: new Opaque("macros.random"),
    dateutil: new Opaque("macros.dateutil"),
  };
  return {
    ds: dsOf(d),
    ds_nodash: strftime(d, "%Y%m%d"),
    ts,
    ts_nodash: strftime(d, "%Y%m%dT%H%M%S"),
    ts_nodash_with_tz: ts.replace(/[-:]/g, ""),
    logical_date: logical,
    data_interval_start: start,
    data_interval_end: end,
    run_id: runId,
    dag: { dag_id: dagId },
    dag_run: {
      dag_id: dagId,
      run_id: runId,
      run_type: input.runType ?? "scheduled",
      logical_date: logical,
      data_interval_start: start,
      data_interval_end: end,
      conf,
    },
    params,
    task: { task_id: taskId, dag_id: dagId },
    ti,
    task_instance: ti,
    task_instance_key_str: `${dagId}__${taskId}__${strftime(d, "%Y%m%d")}`,
    macros,
    var: new Opaque("var"),
    conn: new Opaque("conn"),
    conf: new Opaque("conf"),
    partition_key: new Opaque("partition_key"),
    triggering_asset_events: new Opaque("triggering_asset_events"),
    outlet_events: new Opaque("outlet_events"),
    inlet_events: new Opaque("inlet_events"),
    expanded_ti_count: null,
    test_mode: false,
  };
}

// ----------------------------------------------------------------------------- rendering

type PreviewSegment = {
  kind: "text" | "value" | "unknown" | "statement" | "error";
  text: string;
};

type PreviewResult = {
  /** Fully rendered text (unknown parts as ⟨source⟩). */
  text: string;
  segments: PreviewSegment[];
  /** Syntax / evaluation errors, one per failing expression. */
  errors: string[];
};

/** Renders a template against a sample context. Never throws. */
export function renderPreview(template: string, ctx: SampleContext): PreviewResult {
  const segments: PreviewSegment[] = [];
  const errors: string[] = [];
  for (const part of splitTemplate(template)) {
    if (part.kind === "comment") continue;
    if (part.kind === "text") {
      segments.push({ kind: "text", text: part.text });
      continue;
    }
    if (part.kind === "stmt") {
      segments.push({ kind: "statement", text: `⟨${part.src}⟩` });
      continue;
    }
    try {
      const v = evaluate(parseExpression(part.src), ctx);
      if (v instanceof Opaque) segments.push({ kind: "unknown", text: `⟨${part.src}⟩` });
      else segments.push({ kind: "value", text: pyStr(v) });
    } catch (e) {
      if (e instanceof UnknownValue) {
        segments.push({ kind: "unknown", text: `⟨${part.src}⟩` });
      } else {
        const msg = e instanceof Error ? e.message : String(e);
        errors.push(`${part.src}: ${msg}`);
        segments.push({ kind: "error", text: `⟨${part.src}⟩` });
      }
    }
  }
  return { text: segments.map((s) => s.text).join(""), segments, errors };
}

// Shared with the server-side validation (autoair.validation.jinja): one list for both.
const TIME_VARIABLES: ReadonlySet<string> = new Set(LOGICAL_DATE_NAMES);
const DAG_RUN_TIME_ATTRS: ReadonlySet<string> = new Set(DAG_RUN_LOGICAL_DATE_ATTRS);

/**
 * Logical-date dependent context variables used by a template. Asset-triggered runs have
 * no logical date in Airflow 3: the variables are undefined there (rendering fails) and the
 * `dag_run` date attributes are None.
 */
export function templateTimeVariables(template: string): string[] {
  const found = new Set<string>();
  for (const part of splitTemplate(template)) {
    if (part.kind !== "expr" && part.kind !== "stmt") continue;
    let toks: Tok[];
    try {
      toks = tokenize(part.src);
    } catch {
      continue;
    }
    toks.forEach((t, i) => {
      if (t.t !== "name") return;
      const prev = toks[i - 1];
      const afterDot = prev?.t === "op" && prev.v === ".";
      if (!afterDot && TIME_VARIABLES.has(t.v)) found.add(t.v);
      if (afterDot && DAG_RUN_TIME_ATTRS.has(t.v)) {
        const owner = toks[i - 2];
        if (owner?.t === "name" && owner.v === "dag_run") found.add(`dag_run.${t.v}`);
      }
    });
  }
  return [...found];
}
