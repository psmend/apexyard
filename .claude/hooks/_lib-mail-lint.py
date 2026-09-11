#!/usr/bin/env python3
"""
Deterministic linter for HTML email templates.

## Why this exists, and why it is not an agent

The rules governing HTML email split cleanly in two, and the halves want
opposite tools:

- **Mechanical.** `rgba()` is unsupported in Outlook. An `<img>` without `alt`
  is unreadable when images are blocked. A brand webfont with no fallback
  renders as Times in Gmail. A button whose fill measures below 3:1 against its
  own band has an invisible edge. Every one of these is *decidable* - there is
  a right answer and a regex or a formula finds it every time. A language model
  asked to check thirty of them across twenty templates will get most of them
  right, which is the worst possible outcome: you cannot tell which ones.
- **Judgement.** Whether a headline sounds like the brand, whether the copy is
  padded, whether a message is really transactional or is marketing wearing
  clinical clothes. No regex touches these.

So this file does the first half exactly, and hands the second half to Barid
(`.claude/agents/mail-reviewer.md`) with the mechanical results already in
hand - so the agent spends its attention on judgement instead of badly
re-deriving what a formula settles. Same division of labour Rex already uses
with the Fallow static-analysis pass.

## Two tiers of rule

**Universal** rules are true of HTML email regardless of brand and are ON by
default, so a fresh adopter gets value with no configuration at all.

**Brand** rules - palette, font fallbacks, families, CTA grounds - ship EMPTY.
A check whose config is empty SKIPS and says so. It never guesses a house
style, and it never reports a clean bill of health for a check that did not
run. Silence about an unrun check is how a linter becomes a liability.

Config lives at `.claude/project-config.defaults.json` -> `mail_design`,
overridable in `.claude/project-config.json`.

Exit codes: 0 clean, 1 findings at `error` severity, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def _ops_root(start: str) -> str:
    """Walk up to the apexyard ops root, same contract as the shell hooks."""
    r = os.path.abspath(start)
    while r and r != "/":
        if os.path.isfile(os.path.join(r, ".apexyard-fork")):
            return r
        if os.path.isfile(os.path.join(r, "onboarding.yaml")) and os.path.isfile(
            os.path.join(r, "apexyard.projects.yaml")
        ):
            return r
        r = os.path.dirname(r)
    return os.path.abspath(start)


def load_config(ops_root: str) -> dict:
    """Defaults, then a shallow merge of the adopter override."""
    cfg: dict = {}
    base = os.path.join(ops_root, ".claude", "project-config.defaults.json")
    over = os.path.join(ops_root, ".claude", "project-config.json")
    for path in (base, over):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded.get("mail_design"), dict):
            cfg = loaded["mail_design"]  # shallow: the override replaces wholesale
    return cfg


# --------------------------------------------------------------------------
# colour + contrast (WCAG 2.1 relative luminance)
# --------------------------------------------------------------------------

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _srgb_to_linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


@dataclass
class Finding:
    severity: str  # error | warn | info
    rule: str
    message: str
    path: str = ""
    line: int = 0

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  [{self.severity.upper():5}] {self.rule:28} {where}\n           {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def add(self, *a, **kw) -> None:
        self.findings.append(Finding(*a, **kw))

    def skip(self, rule: str, why: str) -> None:
        self.skipped.append(f"{rule}: {why}")

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


# --------------------------------------------------------------------------
# universal checks
# --------------------------------------------------------------------------

# Quote-aware on purpose. A single character class like ["']([^"']*)["'] breaks
# on a style attribute delimited by one quote style and containing the other -
# which is exactly how a double-quoted font stack is written inside a
# single-quoted attribute.
STYLE_ATTR_RE = re.compile(r"""style\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _style_of(tag: str) -> str:
    return "".join(a or b for a, b in STYLE_ATTR_RE.findall(tag))
IMG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
TD_OPEN_RE = re.compile(r"<t[dh]\b[^>]*>", re.I)


def check_universal(text: str, path: str, uni: dict, rep: Report) -> None:
    def on(key: str) -> bool:
        return bool(uni.get(key, True))

    if on("forbid_alpha_colour"):
        rep.checked.append("alpha-colour")
        for m in re.finditer(r"\b(rgba|hsla)\s*\(", text, re.I):
            rep.add(
                "error",
                "alpha-colour",
                f"`{m.group(1)}()` is not honoured by Outlook. Pre-composite it to a flat "
                "six-digit hex against the surface the element actually sits on.",
                path,
                line_of(text, m.start()),
            )

    if on("forbid_layout_style_block"):
        rep.checked.append("style-block")
        # A <style> block is NOT banned outright, and getting this wrong in
        # either direction costs something real. Ban it entirely and no brand
        # face can ever load, which makes a brand-typeface ruling decorative -
        # an unloaded face resolves only for a reader who happens to have it
        # installed locally, which is almost nobody. Permit it freely and Gmail
        # silently strips the layout rules and the mail falls apart with no
        # error anywhere. So: @font-face may live there, and nothing else.
        for m in re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", text, re.I | re.S):
            body = re.sub(r"@font-face\s*\{[^}]*\}", "", m.group(1), flags=re.I | re.S)
            if body.strip():
                rep.add(
                    "error",
                    "style-block",
                    "A <style> block may contain @font-face and nothing else. Gmail strips "
                    "the rest, silently. Move layout and colour inline onto the elements.",
                    path,
                    line_of(text, m.start()),
                )

    if on("forbid_third_party_fonts"):
        rep.checked.append("font-privacy")
        for pat, what in ((r"@import\b", "`@import`"), (r"<link\b", "`<link>`")):
            for m in re.finditer(pat, text, re.I):
                rep.add(
                    "error",
                    "font-privacy",
                    f"{what} does not load reliably in email, and a stylesheet fetched on "
                    "open is a tracking signal. Use @font-face with a self-hosted src, or "
                    "name the fallback inline and ship without the face.",
                    path,
                    line_of(text, m.start()),
                )
        for m in re.finditer(r"(fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit|fonts\.bunny\.net)", text, re.I):
            rep.add(
                "error",
                "font-privacy",
                f"`{m.group(1)}` is a third-party font host. A font fetched when the "
                "recipient opens the mail tells that host that a specific person opened "
                "specific mail at a specific time. Self-host the face.",
                path,
                line_of(text, m.start()),
            )

    if on("forbid_script"):
        rep.checked.append("script")
        for m in re.finditer(r"<script\b", text, re.I):
            rep.add("error", "script", "Scripts are stripped by every mail client and trip spam filters.", path, line_of(text, m.start()))

    if on("forbid_css_filter"):
        rep.checked.append("css-filter")
        for m in re.finditer(r"filter\s*:", text, re.I):
            rep.add(
                "error",
                "css-filter",
                "`filter:` is unsupported in many clients. A logo knocked out with "
                "brightness/invert degrades to the original artwork on its own ground - "
                "usually black on black. Ship a flat light asset instead.",
                path,
                line_of(text, m.start()),
            )

    if on("require_img_alt") or on("require_img_width"):
        rep.checked.append("img-attrs")
        for m in IMG_RE.finditer(text):
            tag, ln = m.group(0), line_of(text, m.start())
            if on("require_img_alt") and not re.search(r"\balt\s*=", tag, re.I):
                rep.add("error", "img-attrs", "<img> has no alt attribute. Assume images are blocked; decorative images take alt=\"\".", path, ln)
            if on("require_img_width") and not re.search(r"\bwidth\s*=", tag, re.I):
                rep.add("error", "img-attrs", "<img> has no width attribute. Without it Outlook sizes from the file, not the layout.", path, ln)

    if on("require_table_layout"):
        rep.checked.append("table-layout")
        if not re.search(r"<table\b", text, re.I):
            rep.add("error", "table-layout", "No <table> found. Email layout is tables; divs and flexbox break in Outlook.", path, 1)

    if on("forbid_modern_layout"):
        rep.checked.append("modern-layout")
        for m in re.finditer(r"display\s*:\s*(flex|grid|inline-flex|inline-grid)\b", text, re.I):
            rep.add("error", "modern-layout", f"`display:{m.group(1)}` is not supported by Outlook's Word engine.", path, line_of(text, m.start()))

    width = uni.get("expected_width_px")
    if width:
        rep.checked.append("column-width")
        if not re.search(rf"\b{int(width)}\b", text):
            rep.add("warn", "column-width", f"No {int(width)}px column found. The spec's fixed width is what keeps line length predictable.", path, 1)


# --------------------------------------------------------------------------
# brand checks - each SKIPS loudly when unconfigured
# --------------------------------------------------------------------------


def check_palette(text: str, path: str, cfg: dict, rep: Report) -> None:
    palette = cfg.get("palette") or {}
    if not palette:
        rep.skip("palette", "no mail_design.palette configured")
        return
    rep.checked.append("palette")
    known = {v.lower() for v in palette.values() if isinstance(v, str) and v.startswith("#")}
    known |= {v.lower() for v in (cfg.get("allow_hex_outside_palette") or [])}
    seen: set[str] = set()
    for m in HEX_RE.finditer(text):
        hexv = m.group(0).lower()
        if hexv in known or hexv in seen:
            continue
        seen.add(hexv)
        rep.add("error", "palette", f"`{m.group(0)}` is not in the configured palette. Every colour in a template comes from the palette or it is drift.", path, line_of(text, m.start()))


def check_fonts(text: str, path: str, cfg: dict, rep: Report) -> None:
    stacks = ((cfg.get("fonts") or {}).get("stacks")) or []
    if not stacks:
        rep.skip("font-fallback", "no mail_design.fonts.stacks configured")
        return
    rep.checked.append("font-fallback")
    # Scan INSIDE style attributes, then split on ";" only.
    #
    # The first version used one regex over the whole document with a character
    # class that excluded a quote: font-family:[^;"']+ . A stack written
    # font-family:"Playfair Display" therefore produced NO match at all, the
    # loop never ran, and the exact violation this check exists to catch walked
    # straight through. The identical defect was independently written into the
    # sibling guard in the project repo and caught in review there - it survived
    # two separate authorings, so it earns a comment: a character class that
    # excludes a quote cannot see a quoted font name.
    for sm in STYLE_ATTR_RE.finditer(text):
        style = sm.group(1) or sm.group(2) or ""
        for d in re.finditer(r"font-family\s*:([^;]+)", style, re.I):
            decl = d.group(1)
            for entry in stacks:
                face = entry.get("face", "")
                fallbacks = entry.get("requires_fallback") or []
                if not face or face.lower() not in decl.lower():
                    continue
                if not any(fb.lower() in decl.lower() for fb in fallbacks):
                    rep.add(
                        "error",
                        "font-fallback",
                        f"`{face}` is named with none of its fallbacks "
                        f"({', '.join(fallbacks)}) in the same declaration. Clients that "
                        f"strip the webfont render the client default, usually Times - "
                        f"which looks like a bug, not a brand.",
                        path,
                        line_of(text, sm.start()),
                    )


TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b([^>]*)>", re.S)
VOID_TAGS = {"img", "br", "hr", "meta", "link", "input", "source", "area",
             "base", "col", "embed", "param", "track", "wbr"}
BG_RE = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{6})", re.I)
FG_RE = re.compile(r"(?<!-)\bcolor\s*:\s*(#[0-9a-fA-F]{6})", re.I)


@dataclass
class Element:
    offset: int
    tag: str
    fg: str | None
    ground: str | None          # what its TEXT sits on (own background if it has one)
    own_bg: str | None
    parent_ground: str | None   # what the ELEMENT sits on, ignoring its own background
    cell_depth: int             # background-carrying td/th ancestors - see _bands()


def parse_elements(text: str) -> list[Element]:
    """
    Walk the document maintaining a tag stack, so every colour is resolved
    against the background it *actually* sits on.

    This matters more than it looks. Email nests tables constantly - the
    canonical button is a `<td background:orange>` inside a `<td
    background:white>` - so a naive "find the enclosing cell" scan attributes
    the button's white label to the white band and reports 1:1. That false
    positive is worse than no check at all: a linter the team learns to
    disbelieve stops being a gate. The stack is what makes the contrast numbers
    trustworthy.
    """
    stack: list[tuple[str, str | None]] = []
    out: list[Element] = []

    for m in TAG_RE.finditer(text):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)

        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
            continue

        style = _style_of(m.group(0))
        own_bg_m = BG_RE.search(style) or BG_RE.search(attrs)
        own_bg = own_bg_m.group(1).lower() if own_bg_m else None
        fg_m = FG_RE.search(style)
        fg = fg_m.group(1).lower() if fg_m else None

        inherited = next((bg for _, bg in reversed(stack) if bg), None)
        cell_depth = sum(1 for n, bg in stack if bg and n in ("td", "th"))

        if fg or own_bg:
            out.append(Element(m.start(), name, fg, own_bg or inherited,
                               own_bg, inherited, cell_depth))

        if name not in VOID_TAGS and not attrs.rstrip().endswith("/"):
            stack.append((name, own_bg))

    return out


def _bands(text: str) -> list[tuple[int, str, str]]:
    """
    Top-level bands only: (offset, ground, tag).

    A band is a background-carrying **cell** with no background-carrying cell
    above it. Two exclusions, both learned from real templates:

    - The page-ground `<table>` wrapping the whole mail is not a band. Counting
      every background-carrying ancestor made every real band depth 1 and the
      signature-band check reported "none found" on a template that plainly had
      one. Only td/th ancestors count.
    - The orange button is a `<td background:orange>` inside the white body
      cell. It is a component, not a band, so it is excluded by cell depth.
    """
    return [
        (e.offset, e.own_bg, e.tag)
        for e in parse_elements(text)
        if e.own_bg and e.tag in ("td", "th") and e.cell_depth == 0
    ]


def check_contrast(text: str, path: str, cfg: dict, rep: Report) -> None:
    con = cfg.get("contrast") or {}
    floor = float(con.get("text_min", 4.5))
    exceptions = {
        (e.get("fg", "").lower(), e.get("bg", "").lower())
        for e in (con.get("accepted_exceptions") or [])
    }
    rep.checked.append("contrast")
    for el in parse_elements(text):
        if not el.fg or not el.ground:
            continue
        if (el.fg, el.ground) in exceptions:
            continue
        ratio = contrast(el.fg, el.ground)
        if ratio < floor:
            rep.add(
                "error",
                "contrast",
                f"Text {el.fg} on ground {el.ground} measures {ratio:.2f}:1, below the "
                f"{floor}:1 floor. Either lift the text or change the band.",
                path,
                line_of(text, el.offset),
            )


def check_cta(text: str, path: str, cfg: dict, rep: Report) -> None:
    cta = cfg.get("cta") or {}
    fill = (cta.get("fill") or "").lower()
    if not fill:
        rep.skip("cta-ground", "no mail_design.cta.fill configured")
        return
    rep.checked.append("cta-ground")
    allowed = {g.lower() for g in (cta.get("allowed_grounds") or [])}
    non_text = float((cfg.get("contrast") or {}).get("non_text_min", 3.0))

    # Ask what the button SITS ON, not what it is. An earlier refactor changed
    # _bands' third element from inner-HTML to a tag name, and this check kept
    # comparing the fill against it - so it silently compared "#f65802" to "td"
    # and never fired again. parent_ground exists so the question can be asked
    # directly instead of inferred from a tuple position.
    occurrences = 0
    for el in parse_elements(text):
        if el.own_bg != fill:
            continue
        occurrences += 1
        ground = el.parent_ground
        if allowed and ground and ground not in allowed:
            ratio = contrast(fill, ground)
            rep.add(
                "error",
                "cta-ground",
                f"The action colour {fill} sits on ground {ground}, which is not an "
                f"allowed CTA ground. Its fill measures {ratio:.2f}:1 against that band "
                f"(non-text floor is {non_text}:1) - the label can be perfect and the "
                f"button still has no edge.",
                path,
                line_of(text, el.offset),
            )
    cap = cta.get("max_per_email")
    if cap and occurrences > int(cap):
        rep.add("warn", "cta-count", f"{occurrences} call-to-action buttons; the spec allows {cap}.", path, 1)


def check_family(text: str, path: str, cfg: dict, family: str, rep: Report) -> None:
    fams = {k: v for k, v in (cfg.get("families") or {}).items() if not k.startswith("_")}
    if not fams:
        rep.skip("family", "no mail_design.families configured")
        return
    if not family:
        for name, spec in fams.items():
            ground = (spec.get("signature_ground") or "").lower()
            if ground and ground in text.lower():
                family = name
                break
    if not family:
        rep.add("warn", "family", "Could not tell which family this template belongs to - no configured signature ground found. Pass --family to check its rules.", path, 1)
        return
    spec = fams.get(family)
    if not spec:
        rep.add("error", "family", f"Unknown family `{family}`. Configured: {', '.join(fams)}.", path, 1)
        return

    rep.checked.append(f"family:{family}")
    ground = (spec.get("signature_ground") or "").lower()
    if ground:
        count = sum(1 for _, g, _ in _bands(text) if g == ground)
        cap = int(spec.get("max_signature_bands", 1))
        if count == 0:
            rep.add("error", "family", f"No signature band found for family `{family}` (expected ground {ground}).", path, 1)
        elif count > cap:
            rep.add("error", "family", f"{count} signature bands for family `{family}`; the spec allows {cap}. A second one turns a system into decoration.", path, 1)

    if spec.get("requires_photo") and not IMG_RE.search(text):
        rep.add("error", "family", f"Family `{family}` requires a photograph and the template has no <img>.", path, 1)

    if spec.get("allows_unsubscribe") is False:
        for m in re.finditer(r"unsubscribe", text, re.I):
            rep.add(
                "error",
                "family",
                f"Family `{family}` must not carry an unsubscribe link - it would offer an "
                "opt-out from mail the recipient needs.",
                path,
                line_of(text, m.start()),
            )
            break

    # adjacent identical grounds - a band that repeats its neighbour is the same band
    grounds = [g for _, g, _ in _bands(text)]
    for i in range(1, len(grounds)):
        if grounds[i] == grounds[i - 1]:
            rep.add("warn", "band-sequence", f"Two adjacent bands share ground {grounds[i]}. If a section repeats its neighbour's ground, it is the same section.", path, 1)
            break


def check_copy(text: str, path: str, cfg: dict, rep: Report) -> None:
    copy = cfg.get("copy") or {}

    for entry in copy.get("forbidden_patterns") or []:
        pat, msg = entry.get("pattern"), entry.get("message", "Forbidden pattern.")
        if not pat:
            continue
        rep.checked.append("forbidden-pattern")
        for m in re.finditer(pat, text):
            rep.add("error", "forbidden-pattern", msg, path, line_of(text, m.start()))
            break

    words = copy.get("forbidden_words") or []
    if words:
        rep.checked.append("forbidden-word")
        for w in words:
            for m in re.finditer(rf"\b{re.escape(w)}\b", text, re.I):
                rep.add("error", "forbidden-word", f"`{w}` must not appear in a template - including subject, preheader and alt text.", path, line_of(text, m.start()))
                break

    tokens = copy.get("placeholder_tokens") or []
    if tokens:
        rep.checked.append("placeholder")
        for t in tokens:
            for m in re.finditer(re.escape(t), text):
                rep.add("error", "placeholder", f"Placeholder `{t}` is still in the template.", path, line_of(text, m.start()))
                break

    syntax = copy.get("variable_syntax")
    if syntax:
        rep.checked.append("variables")
        used = sorted(set(re.findall(syntax, text)))
        if used:
            rep.add("info", "variables", f"Declares these variables: {', '.join(used)}. Every one must appear in the template's declared variable list - an undeclared token renders as an empty string, silently.", path, 1)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def lint_file(path: str, cfg: dict, family: str, rep: Report) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        rep.add("error", "unreadable", f"Could not read: {exc}", path, 0)
        return
    check_universal(text, path, cfg.get("universal") or {}, rep)
    check_palette(text, path, cfg, rep)
    check_fonts(text, path, cfg, rep)
    check_contrast(text, path, cfg, rep)
    check_cta(text, path, cfg, rep)
    check_family(text, path, cfg, family, rep)
    check_copy(text, path, cfg, rep)


def resolve_targets(args_paths: list[str], cfg: dict, root: str) -> list[str]:
    if args_paths:
        out: list[str] = []
        for p in args_paths:
            out.extend(sorted(glob.glob(p, recursive=True)) if any(c in p for c in "*?[") else [p])
        return [p for p in out if os.path.isfile(p)]
    found: list[str] = []
    for pattern in cfg.get("template_globs") or []:
        found.extend(sorted(glob.glob(os.path.join(root, pattern), recursive=True)))
    return [p for p in found if os.path.isfile(p)]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Lint HTML email templates against the mail design spec.")
    ap.add_argument("paths", nargs="*", help="Template files or globs. Defaults to mail_design.template_globs.")
    ap.add_argument("--family", default="", help="Force a family instead of detecting it from the signature ground.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--root", default=os.getcwd())
    args = ap.parse_args(argv)

    root = _ops_root(args.root)
    cfg = load_config(root)
    if not cfg:
        print("mail-lint: no mail_design config found; nothing to check.", file=sys.stderr)
        return 0

    targets = resolve_targets(args.paths, cfg, root)
    rep = Report()
    if not targets:
        print("mail-lint: no templates matched.", file=sys.stderr)
        return 0

    for path in targets:
        lint_file(path, cfg, args.family, rep)

    if args.json:
        print(json.dumps({
            "templates": targets,
            "findings": [f.__dict__ for f in rep.findings],
            "skipped": rep.skipped,
            "checks_run": sorted(set(rep.checked)),
            "errors": rep.errors,
        }, indent=2))
        return 1 if rep.errors else 0

    print(f"\nmail-lint — {len(targets)} template(s), {len(set(rep.checked))} check(s) run\n")
    for sev in ("error", "warn", "info"):
        group = [f for f in rep.findings if f.severity == sev]
        if group:
            print(f"{sev.upper()} ({len(group)})")
            for f in group:
                print(f.render())
            print()
    if rep.skipped:
        print("SKIPPED — these checks did not run, so this report says nothing about them:")
        for s in rep.skipped:
            print(f"  - {s}")
        print()
    print("clean" if not rep.findings else f"{rep.errors} error(s), {len(rep.findings) - rep.errors} advisory")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
