"""Record and fact grammar (plan §4.2–4.4).

A record is a Markdown file with frontmatter, fact bullets and links:

    ---
    id: pers-june
    type: person
    name: June
    aliases: [june, jj]
    created: 2026-08-02
    updated: 2026-09-26
    ---
    # June
    prose…
    ## Facts
    - (since 2026-08) PM for Lighthouse. #stated ^raw:2026-08-02#14
    ## Links
    - manages [[ws-lighthouse]] (since 2026-08)

Fact grammar:  "- (<validity>) <text> #<trust>[:<conf>] ^<source>[ ^<source>…]"
Validity forms: "2026-09-26" | "since 2026-08" | "2026-03 → 2026-08" | "until 2026-10-15"
A fact is current when today is inside its validity range. Corrections close the
old range instead of deleting (plan §4.3). Round-trip safety: parse → render →
parse is the identity (unit-tested).
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field

# Record types (plan §4.2) and id prefixes.
RECORD_TYPES = {
    "person": "pers",
    "org": "org",
    "preference": "pref",
    "topic": "topic",
    "decision": "dec",
    "workstream": "ws",
    "procedure": "proc",
}

FACTS_SECTION = "## Facts"
LINKS_SECTION = "## Links"
FACT_HEAD = "- ("

# Fixed link relations (plan §4.4).
LINK_RELS = (
    "works-at", "manages", "reports-to", "member-of", "owns",
    "supplies", "depends-on", "related-to", "decided-in", "supersedes",
)

MAX_RECORD_BODY = 8000          # plan §6.4 size cap
NOW_MAX_CHARS = 4000
BRIEF_MAX_CHARS = 8000

_STOPWORDS = frozenset(
    "a an the and or of to in on for with is are was were be i you he she it "
    "we they my your our their this that these those what which who whom how "
    "does do did about".split()
)

_FACT_RE = re.compile(
    r"^-\s*\((?P<val>[^)]+)\)\s+(?P<text>.*?)\s+#(?P<trust>\w+)"
    r"(?::(?P<conf>[01](?:\.\d+)?))?(?:\s+(?P<srcs>\^.*))?$"
)
_SRC_SPLIT = re.compile(r"\s+")
_LINK_RE = re.compile(
    r"^-\s+(?P<rel>[\w-]+)\s+\[\[(?P<id>[^\]]+)\]\]"
    r"(?:\s+\((?P<val>[^)]+)\))?\s*$"
)


def type_dir(rtype: str) -> str:
    """Folder name for a record type under shared/ (record type == folder)."""
    return rtype


def slugify(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug[:48] or "untitled"


def make_id(rtype: str, name: str) -> str:
    return f"{RECORD_TYPES[rtype]}-{slugify(name)}"


def tokenise(text: str) -> list[str]:
    """Lowercase word tokens minus stopwords — miss-logging and alias checks."""
    raw = [t.strip(".,;:!?()[]\"'").lower() for t in text.split()]
    return [t for t in raw if t.isalnum() and len(t) > 1 and t not in _STOPWORDS]


def parse_iso_ts(iso: str) -> float:
    """Epoch seconds for the store's 'YYYY-MM-DDTHH:MM:SSZ' timestamps, else 0.0.

    ``time.mktime`` reads a struct as local time, which shifts every age by the
    machine's UTC offset; ``calendar.timegm`` reads it as the UTC instant the
    string says it is.
    """
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Validity
# ---------------------------------------------------------------------------

def _norm_date(s: str) -> str:
    """'2026-08' → '2026-08-01' so string comparison works."""
    return s if len(s) == 10 else f"{s}-01"


@dataclass
class Validity:
    raw: str                      # exactly as written, round-trips
    start: str | None             # inclusive, YYYY-MM-DD
    end: str | None               # inclusive, YYYY-MM-DD (None = open)
    point: bool                   # a dated event, never expires


def parse_validity(raw: str) -> Validity:
    v = raw.strip()
    if v.startswith("since "):
        return Validity(v, _norm_date(v[6:]), None, False)
    if v.startswith("until "):
        return Validity(v, None, _norm_date(v[6:]), False)
    if "→" in v:
        a, b = (p.strip() for p in v.split("→", 1))
        return Validity(v, _norm_date(a), _norm_date(b), False)
    # point date = event; events stay current forever, as_of filters them
    return Validity(v, None, None, True)


def validity_contains(raw: str, today: str) -> bool:
    """True when today falls inside the validity range. Point facts never expire."""
    v = parse_validity(raw)
    if v.point:
        return True
    if v.start and today < v.start:
        return False
    if v.end and today > v.end:
        return False
    return True


def validity_at(raw: str, as_of: str) -> bool:
    """True when as_of fell inside the validity range (point events already happened)."""
    v = parse_validity(raw)
    if v.point:
        return as_of >= v.raw
    if v.start and as_of < v.start:
        return False
    if v.end and as_of > v.end:
        return False
    return True


# ---------------------------------------------------------------------------
# Fact, Link, Record
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    validity: str
    text: str
    trust: str                            # stated | observed | inferred | external
    sources: list[str] = field(default_factory=list)
    confidence: float | None = None       # for inferred

    def current(self, today: str) -> bool:
        return validity_contains(self.validity, today)

    def at(self, as_of: str) -> bool:
        return validity_at(self.validity, as_of)

    def line(self) -> str:
        conf = "" if self.confidence is None else f":{self.confidence:g}"
        srcs = " ".join(f"^{s}" for s in self.sources)
        return f"- ({self.validity}) {self.text} #{self.trust}{conf} {srcs}".rstrip()


@dataclass
class Link:
    rel: str
    target: str
    validity: str = ""

    def line(self) -> str:
        val = f" ({self.validity})" if self.validity else ""
        return f"- {self.rel} [[{self.target}]]{val}"


@dataclass
class Record:
    id: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    front: dict = field(default_factory=dict)      # decision: status, decided, decided_by
    prose: list[str] = field(default_factory=list)  # lines between title and Facts
    facts: list[Fact] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    decision: dict = field(default_factory=dict)   # {"Decision":..., "Why":..., "Revisit if":...}
    path: str | None = None

    def fact_lines(self, today: str | None = None, as_of: str | None = None,
                   max_lines: int = 0) -> list[str]:
        out = []
        for f in self.facts:
            if today and not f.current(today):
                continue
            if as_of and not f.at(as_of):
                continue
            out.append(f.line())
            if max_lines and len(out) >= max_lines:
                break
        return out

    def current_facts(self, today: str) -> list[Fact]:
        return [f for f in self.facts if f.current(today)]

    def body_chars(self) -> int:
        return len(render_record(self))


def _parse_inline_list(v: str) -> list[str]:
    inner = v.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]


def parse_frontmatter(lines: list[str]) -> tuple[dict, int]:
    """Parse simple frontmatter. Returns (fields, index after closing fence)."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    fields: dict = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return fields, i + 1
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if "[" in val and "]" in val:
            fields[key] = _parse_inline_list(val)
        else:
            fields[key] = val
    return fields, len(lines)


def parse_record(text: str, path: str | None = None) -> Record:
    lines = text.replace("\r\n", "\n").split("\n")
    front, i = parse_frontmatter(lines)
    rec = Record(
        id=str(front.get("id", "")).strip(),
        type=str(front.get("type", "")).strip(),
        name=str(front.get("name", "")).strip(),
        created=str(front.get("created", "")).strip(),
        updated=str(front.get("updated", "")).strip(),
        front=front,
        path=path,
    )
    for key, val in front.items():
        if key not in ("id", "type", "name", "created", "updated"):
            rec.front[key] = val
    if isinstance(front.get("aliases"), list):
        rec.aliases = list(front["aliases"])

    section = "prose"
    body_open = False
    for line in lines[i:]:
        if line.startswith("# "):
            if not rec.name:
                rec.name = line[2:].strip()
            continue                     # title line, never prose
        if line.strip() == FACTS_SECTION:
            section, body_open = "facts", True
            continue
        if line.strip() == LINKS_SECTION:
            section, body_open = "links", True
            continue
        if line.startswith("## ") and line[3:].strip() not in ("Facts", "Links"):
            section = line[3:].strip()
            rec.decision[section] = []
            body_open = True
            continue
        if not line.strip() and not body_open:
            continue
        if line.strip().startswith(FACT_HEAD):
            f = _parse_fact_bullet(line)
            if f:
                rec.facts.append(f)
                continue
        m = _LINK_RE.match(line.strip())
        if m and m.group("rel") in LINK_RELS:
            rec.links.append(Link(m.group("rel"), m.group("id"), m.group("val") or ""))
            continue
        if section in rec.decision:
            if line.strip():
                rec.decision[section].append(line.strip())
        elif section == "prose":
            rec.prose.append(line.rstrip())

    if not rec.id and rec.type and rec.name:
        rec.id = make_id(rec.type, rec.name)
    return rec


def _parse_fact_bullet(line: str) -> Fact | None:
    s = line.strip()
    if not s.startswith(FACT_HEAD):
        return None
    m = _FACT_RE.match(s)
    if not m:
        return None
    srcs = m.group("srcs")
    sources = [p[1:] for p in _SRC_SPLIT.split(srcs) if p.startswith("^")] if srcs else []
    return Fact(
        validity=m.group("val"),
        text=m.group("text"),
        trust=m.group("trust"),
        sources=sources,
        confidence=float(m.group("conf")) if m.group("conf") else None,
    )


def render_record(rec: Record) -> str:
    out = ["---", f"id: {rec.id}", f"type: {rec.type}", f"name: {rec.name}"]
    if rec.aliases:
        out.append(f"aliases: [{', '.join(rec.aliases)}]")
    out += [f"created: {rec.created}", f"updated: {rec.updated}"]
    for key, val in rec.front.items():
        if key in ("id", "type", "name", "created", "updated", "aliases"):
            continue
        if isinstance(val, list):
            out.append(f"{key}: [{', '.join(str(v) for v in val)}]")
        else:
            out.append(f"{key}: {val}")
    out += ["---", "", f"# {rec.name}", ""]
    if rec.prose:
        out += [p for p in rec.prose if p.strip()] + [""]
    if rec.decision:
        for head, lines in rec.decision.items():
            out += [f"## {head}", "", *(lines or [""]), ""]
    if rec.facts:
        out += ["## Facts", *(f.line() for f in rec.facts), ""]
    if rec.links:
        out += ["## Links", *(link.line() for link in rec.links), ""]
    text = "\n".join(out).rstrip("\n") + "\n"
    return text


def close_fact(rec: Record, match: str, end: str, sources: list[str]) -> bool:
    """Close an open fact's range (plan §4.3: correct by closing, not deleting).
    Returns True when a fact was closed."""
    for f in rec.facts:
        v = parse_validity(f.validity)
        if v.point or v.end:
            continue
        if match.lower() in f.text.lower():
            if f.validity.startswith("since "):
                f.validity = f"{f.validity[6:]} → {end}"
            else:
                f.validity = f"until {end}"
            for s in sources:
                if s not in f.sources:
                    f.sources.append(s)
            return True
    return False
