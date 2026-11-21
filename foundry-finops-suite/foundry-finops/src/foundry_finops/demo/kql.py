"""A tiny, dependency-free KQL subset evaluator used by demo mode (and tests).

It is **not** Kusto. It supports enough to explore the demo tables the same way you would
in Log Analytics::

    TokenUsage
    | where TimeGenerated > ago(7d) and Model has "gpt"
    | extend Tokens = InputTokens + OutputTokens
    | summarize Tokens = sum(Tokens), Calls = sum(Requests) by Model, bin(TimeGenerated, 1d)
    | order by TimeGenerated asc
    | render timechart

Operators: where, extend, project, project-away, summarize … by, order/sort by, take/limit,
top N by, distinct, count, render. Functions: ago, now, bin, startofday, round, todouble,
toint, tolong, tostring, tolower, toupper, strcat, iff, isempty, isnotempty, abs, sum, avg,
count, countif, dcount, min, max, make_set.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

Row = dict[str, Any]


class KqlError(ValueError):
    pass


@dataclass
class KqlResult:
    columns: list[str]
    rows: list[list[Any]]
    render: str | None = None


# ------------------------------------------------------------------- tokenizer
_TOKEN = re.compile(
    r"""\s*(?:
    (?P<ts>\d+(?:\.\d+)?(?:d|h|m|s|ms)\b)|
    (?P<num>\d+(?:\.\d+)?)|
    (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|
    (?P<op>==|!=|<>|>=|<=|=~|!~|[-+*/%<>=(),\[\]])|
    (?P<id>[A-Za-z_][A-Za-z0-9_.\-]*(?<![-.]))
    )""",
    re.VERBOSE,
)
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def _tokenize(text: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise KqlError(f"Unexpected input near: {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)  # type: ignore[arg-type]
        if kind == "ts":
            num, unit = re.match(r"([\d.]+)(\w+)", val).groups()  # type: ignore[union-attr]
            out.append(("ts", timedelta(seconds=float(num) * _UNITS[unit])))
        elif kind == "num":
            out.append(("num", float(val) if "." in val else int(val)))
        elif kind == "str":
            out.append(("str", bytes(val[1:-1], "utf-8").decode("unicode_escape")))
        elif kind == "op":
            out.append(("op", val))
        else:
            out.append(("id", val))
    return out


# --------------------------------------------------------------- expression AST
Expr = Callable[[Row], Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(v: Any) -> Any:
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return v
    return v


def _bin(v: Any, size: Any) -> Any:
    v = _to_dt(v)
    if isinstance(v, datetime) and isinstance(size, timedelta):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        secs = size.total_seconds()
        return epoch + timedelta(seconds=math.floor((v - epoch).total_seconds() / secs) * secs)
    if isinstance(v, (int, float)) and isinstance(size, (int, float)):
        return math.floor(v / size) * size
    return v


_SCALAR: dict[str, Callable[..., Any]] = {
    "ago": lambda d: _now() - d,
    "now": lambda: _now(),
    "bin": _bin,
    "floor": _bin,
    "startofday": lambda v: _bin(v, timedelta(days=1)),
    "round": lambda v, n=0: round(float(v), int(n)) if v is not None else None,
    "todouble": lambda v: float(v) if v not in (None, "") else None,
    "toreal": lambda v: float(v) if v not in (None, "") else None,
    "toint": lambda v: int(float(v)) if v not in (None, "") else None,
    "tolong": lambda v: int(float(v)) if v not in (None, "") else None,
    "tostring": lambda v: "" if v is None else (v.isoformat() if isinstance(v, datetime) else str(v)),
    "tolower": lambda v: str(v).lower(),
    "toupper": lambda v: str(v).upper(),
    "strcat": lambda *a: "".join("" if x is None else str(x) for x in a),
    "iff": lambda c, a, b: a if c else b,
    "iif": lambda c, a, b: a if c else b,
    "isempty": lambda v: v in (None, ""),
    "isnotempty": lambda v: v not in (None, ""),
    "isnull": lambda v: v is None,
    "isnotnull": lambda v: v is not None,
    "abs": lambda v: abs(v),
    "todatetime": _to_dt,
}

_AGGS = {"sum", "avg", "count", "countif", "dcount", "min", "max", "make_set", "sumif", "avgif"}


def _cmp(op: str, a: Any, b: Any) -> bool:
    a, b = _to_dt(a), _to_dt(b)
    if op in ("==",):
        return bool(a == b)
    if op in ("!=", "<>"):
        return bool(a != b)
    if op == "=~":
        return str(a).lower() == str(b).lower()
    if op == "!~":
        return str(a).lower() != str(b).lower()
    if a is None or b is None:
        return False
    try:
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
    except TypeError:
        return False


_WORD_OPS = {"contains", "!contains", "has", "!has", "startswith", "endswith", "in", "!in", "between"}


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]) -> None:
        self.t = tokens
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, Any] | None:
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def take(self) -> tuple[str, Any]:
        tok = self.peek()
        if tok is None:
            raise KqlError("Unexpected end of expression")
        self.i += 1
        return tok

    def accept(self, kind: str, val: Any = None) -> bool:
        tok = self.peek()
        if tok and tok[0] == kind and (val is None or str(tok[1]).lower() == str(val).lower()):
            self.i += 1
            return True
        return False

    def expect(self, kind: str, val: Any = None) -> None:
        if not self.accept(kind, val):
            raise KqlError(f"Expected {val or kind} near token {self.peek()}")

    def done(self) -> bool:
        return self.i >= len(self.t)

    # grammar --------------------------------------------------------------
    def expr(self) -> Expr:
        left = self.and_()
        while self.accept("id", "or"):
            right = self.and_()
            left = (lambda l, r: lambda row: bool(l(row)) or bool(r(row)))(left, right)
        return left

    def and_(self) -> Expr:
        left = self.not_()
        while self.accept("id", "and"):
            right = self.not_()
            left = (lambda l, r: lambda row: bool(l(row)) and bool(r(row)))(left, right)
        return left

    def not_(self) -> Expr:
        if self.peek() == ("id", "not") and self.peek(1) == ("op", "("):
            self.take()
            inner = self.primary()
            return lambda row: not inner(row)
        return self.cmp()

    def cmp(self) -> Expr:
        left = self.add()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("==", "!=", "<>", ">", "<", ">=", "<=", "=~", "!~"):
            self.take()
            right = self.add()
            return (lambda o, l, r: lambda row: _cmp(o, l(row), r(row)))(tok[1], left, right)
        if tok and tok[0] == "id" and tok[1].lower().lstrip("!") in {w.lstrip("!") for w in _WORD_OPS}:
            word = tok[1].lower()
            self.take()
            neg = word.startswith("!")
            word = word.lstrip("!")
            if word in ("in",):
                self.expect("op", "(")
                items = [self.add()]
                while self.accept("op", ","):
                    items.append(self.add())
                self.expect("op", ")")
                f = (lambda l, its: lambda row: l(row) in [x(row) for x in its])(left, items)
            elif word == "between":
                raise KqlError("between isn't supported in demo KQL; use >= and <=")
            else:
                right = self.add()
                fn = {
                    "contains": lambda a, b: str(b).lower() in str(a).lower(),
                    "has": lambda a, b: str(b).lower() in str(a).lower(),
                    "startswith": lambda a, b: str(a).lower().startswith(str(b).lower()),
                    "endswith": lambda a, b: str(a).lower().endswith(str(b).lower()),
                }[word]
                f = (lambda fn_, l, r: lambda row: fn_(l(row), r(row)))(fn, left, right)
            return (lambda g: lambda row: not g(row))(f) if neg else f
        return left

    def add(self) -> Expr:
        left = self.mul()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in "+-" and len(tok[1]) == 1:
            self.take()
            right = self.mul()
            left = (lambda o, l, r: lambda row: _arith(o, l(row), r(row)))(tok[1], left, right)
        return left

    def mul(self) -> Expr:
        left = self.unary()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in ("*", "/", "%"):
            self.take()
            right = self.unary()
            left = (lambda o, l, r: lambda row: _arith(o, l(row), r(row)))(tok[1], left, right)
        return left

    def unary(self) -> Expr:
        if self.accept("op", "-"):
            inner = self.unary()
            return lambda row: -inner(row)
        return self.primary()

    def primary(self) -> Expr:
        kind, val = self.take()
        if kind in ("num", "str", "ts"):
            return lambda row, v=val: v
        if kind == "op" and val == "(":
            e = self.expr()
            self.expect("op", ")")
            return e
        if kind == "id":
            low = val.lower()
            if low in ("true", "false"):
                return lambda row, v=(low == "true"): v
            if low == "datetime" and self.accept("op", "("):
                parts = []
                while not self.accept("op", ")"):
                    parts.append(str(self.take()[1]))
                lit = _to_dt("".join(parts))
                return lambda row: lit
            if self.accept("op", "("):
                args: list[Expr] = []
                if not self.accept("op", ")"):
                    args.append(self.expr())
                    while self.accept("op", ","):
                        args.append(self.expr())
                    self.expect("op", ")")
                fn = _SCALAR.get(low)
                if fn is None:
                    raise KqlError(f"Unknown function {val}()")
                return lambda row, f=fn, a=args: f(*[x(row) for x in a])
            if self.accept("op", "["):  # customDimensions["x"]
                key = self.take()[1]
                self.expect("op", "]")
                return lambda row, c=val, k=key: (row.get(c) or {}).get(k) if isinstance(row.get(c), dict) else None
            return lambda row, c=val: row.get(c)
        raise KqlError(f"Unexpected token {val!r}")


def _arith(op: str, a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    a, b = _to_dt(a), _to_dt(b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b if b else None
    return a % b


def compile_expr(text: str) -> Expr:
    p = _Parser(_tokenize(text))
    e = p.expr()
    if not p.done():
        raise KqlError(f"Unexpected token {p.peek()} in {text!r}")
    return e


# --------------------------------------------------------------- operators
def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur, quote = [], 0, "", None
    for ch in text:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _split_pipes(query: str) -> list[str]:
    lines = [ln for ln in query.splitlines() if not ln.strip().startswith("//")]
    return _split_top(" ".join(lines), "|")


def _assignment(item: str) -> tuple[str | None, str]:
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.+)$", item, re.S)
    return (m.group(1), m.group(2)) if m else (None, item)


def _default_name(expr: str) -> str:
    m = re.match(r"^\s*(\w+)\s*\(\s*([A-Za-z_][\w.]*)?", expr)
    if m and m.group(1).lower() in _AGGS:
        return f"{m.group(1).lower()}_{m.group(2)}" if m.group(2) else m.group(1).lower() + "_"
    if m and m.group(1).lower() == "bin" and m.group(2):
        return m.group(2)
    return re.sub(r"\W+", "_", expr.strip()).strip("_") or "Column1"


def _aggregate(expr: str, rows: list[Row]) -> Any:
    m = re.match(r"^\s*(\w+)\s*\((.*)\)\s*$", expr, re.S)
    if not m or m.group(1).lower() not in _AGGS:
        raise KqlError(f"Unsupported aggregation: {expr}")
    fn, arg = m.group(1).lower(), m.group(2).strip()
    if fn == "count":
        return len(rows)
    if fn in ("countif", "sumif", "avgif"):
        args = _split_top(arg)
        if fn == "countif":
            cond = compile_expr(args[0])
            return sum(1 for r in rows if cond(r))
        val, cond = compile_expr(args[0]), compile_expr(args[1])
        vals = [val(r) for r in rows if cond(r) and val(r) is not None]
        return (sum(vals) if fn == "sumif" else (sum(vals) / len(vals) if vals else None))
    e = compile_expr(arg)
    vals = [v for v in (e(r) for r in rows) if v is not None]
    if fn == "sum":
        return round(sum(vals), 6) if any(isinstance(v, float) for v in vals) else sum(vals)
    if fn == "avg":
        return round(sum(vals) / len(vals), 6) if vals else None
    if fn == "min":
        return min(vals) if vals else None
    if fn == "max":
        return max(vals) if vals else None
    if fn == "dcount":
        return len(set(map(str, vals)))
    return sorted(set(map(str, vals)))  # make_set


def run(query: str, tables: Mapping[str, Sequence[Row]]) -> KqlResult:
    """Evaluate ``query`` against in-memory ``tables`` (name → list of row dicts)."""
    stages = _split_pipes(query)
    if not stages:
        raise KqlError("Empty query")
    source = stages[0].strip()
    match = {k.lower(): k for k in tables}
    if source.lower() not in match:
        raise KqlError(f"Unknown table '{source}'. Available: {', '.join(tables)}")
    rows: list[Row] = [dict(r) for r in tables[match[source.lower()]]]
    columns: list[str] = list(rows[0].keys()) if rows else []
    render = None

    for stage in stages[1:]:
        m = re.match(r"^\s*([a-z\-]+)\s*(.*)$", stage, re.S | re.I)
        if not m:
            raise KqlError(f"Can't parse: {stage}")
        op, rest = m.group(1).lower(), m.group(2).strip()
        if op == "where":
            f = compile_expr(rest)
            rows = [r for r in rows if f(r)]
        elif op == "extend":
            for item in _split_top(rest):
                name, ex = _assignment(item)
                name = name or _default_name(ex)
                f = compile_expr(ex)
                for r in rows:
                    r[name] = f(r)
                if name not in columns:
                    columns.append(name)
        elif op in ("project", "project-reorder"):
            specs = []
            for item in _split_top(rest):
                name, ex = _assignment(item)
                specs.append((name or ex.strip(), compile_expr(ex)))
            rows = [{n: f(r) for n, f in specs} for r in rows]
            columns = [n for n, _ in specs]
        elif op == "project-away":
            drop = {c.strip() for c in _split_top(rest)}
            columns = [c for c in columns if c not in drop]
            rows = [{c: r.get(c) for c in columns} for r in rows]
        elif op == "summarize":
            parts = re.split(r"\bby\b", rest, maxsplit=1, flags=re.I)
            agg_part, by_part = parts[0], (parts[1] if len(parts) > 1 else "")
            aggs = [(_assignment(a)[0] or _default_name(_assignment(a)[1]), _assignment(a)[1])
                    for a in _split_top(agg_part)]
            keys = []
            for b in _split_top(by_part):
                name, ex = _assignment(b)
                keys.append((name or _default_name(ex), compile_expr(ex)))
            groups: dict[tuple[Any, ...], list[Row]] = {}
            for r in rows:
                k = tuple(f(r) for _, f in keys)
                groups.setdefault(k, []).append(r)
            if not keys and not groups:
                groups[()] = []
            rows = []
            for k, grp in groups.items():
                out = {n: v for (n, _), v in zip(keys, k)}
                for n, ex in aggs:
                    out[n] = _aggregate(ex, grp)
                rows.append(out)
            columns = [n for n, _ in keys] + [n for n, _ in aggs]
        elif op in ("order", "sort"):
            spec = re.sub(r"^by\s+", "", rest, flags=re.I)
            for item in reversed(_split_top(spec)):
                parts = item.split()
                col, direction = parts[0], (parts[1].lower() if len(parts) > 1 else "desc")
                f = compile_expr(col)
                rows.sort(key=lambda r, f=f: (f(r) is None, _sort_key(f(r))), reverse=direction == "desc")
        elif op in ("take", "limit"):
            rows = rows[: int(rest)]
        elif op == "top":
            mt = re.match(r"(\d+)\s+by\s+(.+?)(?:\s+(asc|desc))?$", rest, re.I)
            if not mt:
                raise KqlError("Usage: top N by Column [asc|desc]")
            f = compile_expr(mt.group(2))
            rows.sort(key=lambda r: _sort_key(f(r)), reverse=(mt.group(3) or "desc").lower() == "desc")
            rows = rows[: int(mt.group(1))]
        elif op == "distinct":
            cols = [c.strip() for c in _split_top(rest)]
            seen, out_rows = set(), []
            for r in rows:
                key = tuple(str(r.get(c)) for c in cols)
                if key not in seen:
                    seen.add(key)
                    out_rows.append({c: r.get(c) for c in cols})
            rows, columns = out_rows, cols
        elif op == "count":
            rows, columns = [{"Count": len(rows)}], ["Count"]
        elif op == "render":
            render = rest.split()[0].lower() if rest else "table"
        else:
            raise KqlError(f"Operator '{op}' isn't supported in demo mode")

    def fmt(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(v, timedelta):
            return str(v)
        return v

    return KqlResult(columns, [[fmt(r.get(c)) for c in columns] for r in rows], render)


def _sort_key(v: Any) -> Any:
    v = _to_dt(v)
    if isinstance(v, datetime):
        return v.timestamp()
    if isinstance(v, (int, float)):
        return v
    return str(v)
