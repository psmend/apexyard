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
import signal
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache

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


class ConfigError(Exception):
    """The adopter's override config exists but cannot be used."""


def load_config(ops_root: str) -> dict:
    """
    Defaults, then a shallow merge of the adopter override.

    **The override fails CLOSED.** An earlier version caught OSError and
    JSONDecodeError on both files and continued, which produced the worst
    possible failure: a truncated `project-config.json` silently dropped the
    adopter's brand rules back to the framework defaults, the palette check
    vanished, and the only trace was a line in the SKIPPED list that reads
    *identically* to "you never configured this". A security review reproduced
    it - same template, 3 errors with a valid config and 1 with a truncated one,
    and on a template with no universal violations the job would have gone
    green. A config you cannot parse is not a config you may quietly replace
    with something looser.

    The DEFAULTS file is different and still tolerant: its absence is a normal
    state for a partial checkout, and it grants nothing the adopter did not
    already have.
    """
    cfg: dict = {}
    base = os.path.join(ops_root, ".claude", "project-config.defaults.json")
    over = os.path.join(ops_root, ".claude", "project-config.json")

    if os.path.isfile(base):
        try:
            with open(base, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("top level is not an object")
            if isinstance(loaded.get("mail_design"), dict):
                cfg = loaded["mail_design"]
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # tolerated: absent or unreadable defaults grant nothing

    if os.path.isfile(over):
        try:
            with open(over, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"{over} exists but could not be read: {exc}. Refusing to fall back to "
                "framework defaults - that would silently drop your brand rules and "
                "report a pass it did not earn. Fix the file, or remove it."
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"{over} parses as JSON but its top level is {type(loaded).__name__}, not an "
                "object. Refusing to fall back to defaults - see above."
            )
        if isinstance(loaded.get("mail_design"), dict):
            cfg = loaded["mail_design"]  # shallow: the override replaces wholesale
    return cfg


# --------------------------------------------------------------------------
# colour + contrast (WCAG 2.1 relative luminance)
# --------------------------------------------------------------------------

# Both hex spellings. Requiring six digits made `color:#ddd` on
# `background-color:#eee` - off-palette and 1.2:1 - report clean, and the
# three-digit form is ubiquitous. Everything downstream normalises via
# `_norm_hex` so the rest of the file only ever sees six digits.
#
# The SIX-digit branch is first on purpose. Alternation is ordered, so with
# the short branch first `#131110` matched `#131` and normalised to
# `#113311` - a colour that appears nowhere in the document, reported against
# a real line number. Caught by the suite immediately; it would have been
# very hard to spot in the wild.
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _norm_hex(h: str) -> str:
    """#abc -> #aabbcc, lowercased. Idempotent on the six-digit form."""
    h = h.strip().lower()
    if len(h) == 4:
        return "#" + "".join(c * 2 for c in h[1:])
    return h


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
        # A newline in a path or an echoed capture would put the next line at
        # column 0 of the CI step's stdout, which is where GitHub Actions looks
        # for ::workflow-commands::. Keep every finding on the lines we chose.
        def _flat(s: str) -> str:
            return re.sub(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]", "\u2423", str(s))

        where = f"{_flat(self.path)}:{self.line}" if self.line else _flat(self.path)
        return f"  [{self.severity.upper():5}] {self.rule:28} {where}\n           {_flat(self.message)}"


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


# Adopter-supplied regexes run against at most this much text. A security
# review confirmed catastrophic backtracking on a config-supplied `(a+)+$`
# against 40 characters - it ran past 25 seconds and was still going. A cap
# does not make a pathological pattern linear, but it bounds the damage to
# something a job timeout survives, and the compile guard below turns the
# other half (a malformed pattern) from an uncaught traceback into a finding.
REGEX_INPUT_CAP = 200_000
# Files larger than this are not linted. Nothing legitimate approaches it - a
# 600px email is a few KB - and an unbounded read is how a single file stalls
# a runner.
MAX_TEMPLATE_BYTES = 2_000_000


# Env-overridable so the regression test can pin the guard without paying the
# full ceiling in wall-clock. The guard itself is not optional.
REGEX_TIMEOUT_SECONDS = max(1, int(os.environ.get("MAIL_LINT_REGEX_TIMEOUT") or 5))


class _RegexTimeout(Exception):
    pass


@contextmanager
def _time_limit(seconds: int):
    """
    Wall-clock ceiling for one config-supplied regex.

    An input cap is NOT sufficient here and it was the first thing tried. A
    security review confirmed `(a+)+$` against **40 characters** backtracking
    past 25 seconds - the blowup is exponential in the input, so capping the
    input at 200KB bounds nothing that matters. Only a clock does.

    SIGALRM is main-thread Unix only. Where it is unavailable the guard
    degrades to nothing rather than failing the run, because a linter that
    refuses to start on an unusual platform is worse than one that can be hung
    by a config the adopter wrote themselves.
    """
    # `hasattr(signal, "SIGALRM")` is a PLATFORM test, and the promise above is
    # about availability. Off the main thread signal.signal raises ValueError,
    # which _run_guarded does not catch - so the docstring's "degrades to
    # nothing" was not true of the most likely way to reach it.
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _fire(signum, frame):
        raise _RegexTimeout()

    previous = signal.signal(signal.SIGALRM, _fire)
    # Whatever the caller had armed, minus what we are about to spend. An
    # unconditional alarm(0) in the finally silently cancelled an outer
    # watchdog - fine for the CLI, very hard to debug in-process.
    outer = signal.alarm(seconds)
    try:
        yield
    finally:
        spent = signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if outer:
            remaining = outer - (seconds - spent) if spent else outer - seconds
            signal.alarm(max(1, remaining))


def _run_guarded(rx, text: str, where: str, path: str, rep: Report, finder="finditer"):
    """Run a config-supplied regex under the clock, reporting a hang as a finding."""
    try:
        with _time_limit(REGEX_TIMEOUT_SECONDS):
            return list(getattr(rx, finder)(text))
    except _RegexTimeout:
        rep.add(
            "error",
            "config",
            f"`{where}` did not finish within {REGEX_TIMEOUT_SECONDS}s and was abandoned - "
            "almost certainly catastrophic backtracking (nested quantifiers such as "
            "`(a+)+`). Rewrite the pattern. This is a CONFIG defect, not a template one.",
            path,
            0,
        )
        return []


def _safe_compile(pattern: str, where: str, path: str, rep: Report):
    """Compile config-supplied regex, reporting rather than crashing."""
    try:
        return re.compile(pattern)
    except (re.error, RecursionError) as exc:
        rep.add(
            "error",
            "config",
            f"`{where}` is not a valid regular expression ({exc}). Fix the pattern in "
            "`.claude/project-config.json`. This is a CONFIG defect, not a template one.",
            path,
            0,
        )
        return None


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


# Every universal rule, so an OFF one can be named rather than silently absent.
UNIVERSAL_RULES = (
    "forbid_alpha_colour", "forbid_layout_style_block", "forbid_third_party_fonts",
    "forbid_script", "forbid_css_filter", "require_img_alt", "require_img_width",
    "require_table_layout", "forbid_modern_layout",
)


def check_universal(text: str, path: str, uni: dict, rep: Report) -> None:
    def on(key: str) -> bool:
        return bool(uni.get(key, True))

    # The skip-loudly contract applied to itself. Four brand checks honoured it
    # and nothing else did, so a config with every universal rule turned off
    # produced "clean" with no SKIPPED section - and the CI summary went on to
    # print "All configured checks ran." That is exactly the liability this
    # module's docstring claims to avoid, so it is now enforced for every rule
    # rather than asserted in prose.
    for key in UNIVERSAL_RULES:
        if not on(key):
            rep.skip(key, f"universal.{key} is disabled in config")
    if not uni.get("expected_width_px"):
        rep.skip("column-width", "no universal.expected_width_px configured")

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
        # Cheap pre-check first. The `.*?` scan rescans to end-of-input from
        # every failed start position, so a document of 20,000 unclosed
        # `<style>` tags measured 47 seconds. If there is no closing tag at all
        # there is nothing to find, and the expensive scan is skipped.
        if not re.search(r"</style", text, re.I):
            if re.search(r"<style\b", text, re.I):
                rep.add(
                    "error",
                    "style-block",
                    "A <style> tag is opened and never closed. Mail clients recover from "
                    "this unpredictably, and the linter cannot tell what is inside it.",
                    path,
                    line_of(text, re.search(r"<style\b", text, re.I).start()),
                )
            return_early_style = True
        else:
            return_early_style = False
        # The empty-list arm is a `continue` for a loop that has no `continue`:
        # the unclosed-<style> branch above has already reported, and running the
        # `.*?` scan anyway is the 47-second path it exists to avoid. Written as
        # a conditional iterable so the reporting and the skip stay adjacent.
        for m in ([] if return_early_style else re.finditer(r"<style\b[^>]*>(.*?)</style\s*>", text, re.I | re.S)):
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
    known = {_norm_hex(v) for v in palette.values() if isinstance(v, str) and v.startswith("#")}
    known |= {_norm_hex(v) for v in (cfg.get("allow_hex_outside_palette") or [])}
    seen: set[str] = set()
    for m in HEX_RE.finditer(text):
        hexv = _norm_hex(m.group(0))
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
# CSS named colours. Reading only hex made the contrast layer blind to
# `bgcolor="white"` - the single most common ground spelling in hand-written
# email - and a 1.03:1 body reported clean while the report still claimed the
# contrast check had run. A colour grammar the linter cannot read is worse
# than one it rejects, because the first is silent.
_NAMED = {
    "aliceblue": "#f0f8ff", "antiquewhite": "#faebd7", "aqua": "#00ffff",
    "aquamarine": "#7fffd4", "azure": "#f0ffff", "beige": "#f5f5dc",
    "bisque": "#ffe4c4", "black": "#000000", "blanchedalmond": "#ffebcd",
    "blue": "#0000ff", "blueviolet": "#8a2be2", "brown": "#a52a2a",
    "burlywood": "#deb887", "cadetblue": "#5f9ea0", "chartreuse": "#7fff00",
    "chocolate": "#d2691e", "coral": "#ff7f50", "cornflowerblue": "#6495ed",
    "cornsilk": "#fff8dc", "crimson": "#dc143c", "cyan": "#00ffff",
    "darkblue": "#00008b", "darkcyan": "#008b8b", "darkgoldenrod": "#b8860b",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "darkgreen": "#006400",
    "darkkhaki": "#bdb76b", "darkmagenta": "#8b008b", "darkolivegreen": "#556b2f",
    "darkorange": "#ff8c00", "darkorchid": "#9932cc", "darkred": "#8b0000",
    "darksalmon": "#e9967a", "darkseagreen": "#8fbc8f", "darkslateblue": "#483d8b",
    "darkslategray": "#2f4f4f", "darkslategrey": "#2f4f4f", "darkturquoise": "#00ced1",
    "darkviolet": "#9400d3", "deeppink": "#ff1493", "deepskyblue": "#00bfff",
    "dimgray": "#696969", "dimgrey": "#696969", "dodgerblue": "#1e90ff",
    "firebrick": "#b22222", "floralwhite": "#fffaf0", "forestgreen": "#228b22",
    "fuchsia": "#ff00ff", "gainsboro": "#dcdcdc", "ghostwhite": "#f8f8ff",
    "gold": "#ffd700", "goldenrod": "#daa520", "gray": "#808080",
    "grey": "#808080", "green": "#008000", "greenyellow": "#adff2f",
    "honeydew": "#f0fff0", "hotpink": "#ff69b4", "indianred": "#cd5c5c",
    "indigo": "#4b0082", "ivory": "#fffff0", "khaki": "#f0e68c",
    "lavender": "#e6e6fa", "lavenderblush": "#fff0f5", "lawngreen": "#7cfc00",
    "lemonchiffon": "#fffacd", "lightblue": "#add8e6", "lightcoral": "#f08080",
    "lightcyan": "#e0ffff", "lightgoldenrodyellow": "#fafad2", "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3", "lightgreen": "#90ee90", "lightpink": "#ffb6c1",
    "lightsalmon": "#ffa07a", "lightseagreen": "#20b2aa", "lightskyblue": "#87cefa",
    "lightslategray": "#778899", "lightslategrey": "#778899", "lightsteelblue": "#b0c4de",
    "lightyellow": "#ffffe0", "lime": "#00ff00", "limegreen": "#32cd32",
    "linen": "#faf0e6", "magenta": "#ff00ff", "maroon": "#800000",
    "mediumaquamarine": "#66cdaa", "mediumblue": "#0000cd", "mediumorchid": "#ba55d3",
    "mediumpurple": "#9370db", "mediumseagreen": "#3cb371", "mediumslateblue": "#7b68ee",
    "mediumspringgreen": "#00fa9a", "mediumturquoise": "#48d1cc",
    "mediumvioletred": "#c71585", "midnightblue": "#191970", "mintcream": "#f5fffa",
    "mistyrose": "#ffe4e1", "moccasin": "#ffe4b5", "navajowhite": "#ffdead",
    "navy": "#000080", "oldlace": "#fdf5e6", "olive": "#808000",
    "olivedrab": "#6b8e23", "orange": "#ffa500", "orangered": "#ff4500",
    "orchid": "#da70d6", "palegoldenrod": "#eee8aa", "palegreen": "#98fb98",
    "paleturquoise": "#afeeee", "palevioletred": "#db7093", "papayawhip": "#ffefd5",
    "peachpuff": "#ffdab9", "peru": "#cd853f", "pink": "#ffc0cb",
    "plum": "#dda0dd", "powderblue": "#b0e0e6", "purple": "#800080",
    "rebeccapurple": "#663399", "red": "#ff0000", "rosybrown": "#bc8f8f",
    "royalblue": "#4169e1", "saddlebrown": "#8b4513", "salmon": "#fa8072",
    "sandybrown": "#f4a460", "seagreen": "#2e8b57", "seashell": "#fff5ee",
    "sienna": "#a0522d", "silver": "#c0c0c0", "skyblue": "#87ceeb",
    "slateblue": "#6a5acd", "slategray": "#708090", "slategrey": "#708090",
    "snow": "#fffafa", "springgreen": "#00ff7f", "steelblue": "#4682b4",
    "tan": "#d2b48c", "teal": "#008080", "thistle": "#d8bfd8",
    "tomato": "#ff6347", "turquoise": "#40e0d0", "violet": "#ee82ee",
    "wheat": "#f5deb3", "white": "#ffffff", "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00", "yellowgreen": "#9acd32",
}

# Values that legitimately declare no colour of their own - the element simply
# sits on whatever its ancestor provides. These must resolve to "nothing here",
# never to an unresolved-colour finding.
_NO_COLOUR = {"transparent", "inherit", "initial", "unset", "none", "currentcolor", "auto"}

_COLOUR_TOKEN = re.compile(r"#[0-9a-fA-F]+|(?:rgba?|hsla?)\s*\([^)]*\)|[a-zA-Z][a-zA-Z]{2,24}", re.I)


def _resolve_colour(tok: str) -> str | None:
    """One colour token -> #rrggbb, or None if it is not a colour we can trust."""
    t = tok.strip().lower()
    if t.startswith("#"):
        # Length is the whole point. `#12345` used to normalise to `#112233` -
        # a colour appearing nowhere in the document - and `#ff0000ff` had its
        # alpha silently dropped, both then reported as MEASURED ratios.
        return _norm_hex(t) if len(t) in (4, 7) else None
    if t in _NAMED:
        return _NAMED[t]
    m = re.match(r"(rgba?|hsla?)\s*\(([^)]*)\)", t)
    if m:
        fn, body = m.group(1), m.group(2)
        parts = [p.strip() for p in body.replace("/", ",").split(",") if p.strip()]
        if len(parts) == 1:
            parts = body.split()
        if len(parts) < 3:
            return None
        # A colour with an alpha channel is NOT a colour you can measure a
        # contrast ratio against - what renders depends on the backdrop. The
        # alpha check already names these; resolving them to their opaque
        # component would hand back a fabricated "measured" number.
        if len(parts) > 3:
            return None
        try:
            if fn.startswith("rgb"):
                vals = [round(float(p[:-1]) * 255 / 100) if p.endswith("%") else int(float(p))
                        for p in parts]
                if not all(0 <= v <= 255 for v in vals):
                    return None
                return "#%02x%02x%02x" % tuple(vals)
            h = float(re.sub(r"deg$", "", parts[0])) % 360 / 360
            s = float(parts[1].rstrip("%")) / 100
            ll = float(parts[2].rstrip("%")) / 100
            if not (0 <= s <= 1 and 0 <= ll <= 1):
                return None
            import colorsys
            r, g, b = colorsys.hls_to_rgb(h, ll, s)
            return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        except ValueError:
            return None
    return None


def _colour_in(value: str) -> tuple[str | None, str | None]:
    """
    First trustworthy colour in a declaration value, plus the first token that
    looked like a colour and was NOT trustworthy.

    Returning the unresolved token matters as much as returning the colour:
    dropping it silently is how `bgcolor="white"` became invisible.
    `background: url(hero.png) #FFFDF9` is why this scans the whole value
    rather than only the first token after the colon.
    """
    unresolved = None
    for m in _COLOUR_TOKEN.finditer(value):
        tok = m.group(0)
        if tok.strip().lower() in _NO_COLOUR:
            continue
        got = _resolve_colour(tok)
        if got:
            return got, None
        if unresolved is None and (tok.startswith("#") or "(" in tok or tok.lower() in _NAMED):
            unresolved = tok
    return None, unresolved


_VALUE = r"([^;\"']+)"
BG_RE = re.compile(rf"background(?:-color)?\s*:\s*{_VALUE}", re.I)
FG_RE = re.compile(rf"(?<!-)\bcolor\s*:\s*{_VALUE}", re.I)
# The PRESENTATIONAL attribute forms. `bgcolor="#FFFDF9"` is the attribute
# Outlook actually honours and the one every email framework emits, so reading
# only the CSS form made the linter blind on the most common authoring style -
# it missed a 1.6:1 body and a CTA on the wrong band, AND raised a false
# "no signature band" on a template whose band was right there. Failing in both
# directions at once is the worst thing a linter can do.
BG_ATTR_RE = re.compile(r"\bbgcolor\s*=\s*(?:\"([^\"]*)\"|\'([^\']*)\'|([^\s>]+))", re.I)
FG_ATTR_RE = re.compile(r"<font\b[^>]*?\bcolor\s*=\s*(?:\"([^\"]*)\"|\'([^\']*)\'|([^\s>]+))", re.I)


def _attr_value(m: re.Match) -> str:
    """First non-empty group of the quoted/unquoted attribute alternation."""
    return next((g for g in m.groups() if g is not None), "")


@dataclass
class Element:
    offset: int
    tag: str
    fg: str | None
    ground: str | None          # what its TEXT sits on (own background if it has one)
    own_bg: str | None
    parent_ground: str | None   # what the ELEMENT sits on, ignoring its own background
    cell_depth: int             # background-carrying td/th ancestors - see _bands()
    unresolved: tuple[str, ...] = ()   # colour values declared here but not readable


def _comment_spans(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    """
    Find the spans a mail client would treat as comments - and only those.

    The naive `<!--.*?-->` this replaced could be opened from inside a quoted
    attribute value, where `<!--` is literal text to every real client. That
    let an author blank arbitrary markup out of the linter's view and ship a
    1.12:1 body with the gate green - the "quietly switch the gate off"
    failure this module exists to prevent. So the scan walks tags properly and
    only opens a comment at document level.

    Returns the spans plus the offsets of any unterminated `<!--`. An
    unterminated opener is NOT treated as a comment: doing so would swallow
    the rest of the document and hand back the same suppression primitive
    through a shorter door. The caller reports it instead.
    """
    spans: list[tuple[int, int]] = []
    unterminated: list[int] = []
    i, n = 0, len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            break
        if text.startswith("<!--", lt):
            end = text.find("-->", lt + 4)
            if end < 0:
                unterminated.append(lt)
                break
            spans.append((lt, end + 3))
            i = end + 3
            continue
        nxt = text[lt + 1] if lt + 1 < n else ""
        if not (nxt.isalpha() or nxt in "/!?"):
            # A bare `<` in prose. Not a tag; stepping over it keeps a later
            # real comment visible.
            i = lt + 1
            continue
        j, quote = lt + 1, ""
        while j < n:
            c = text[j]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c == ">":
                break
            j += 1
        i = j + 1
    return spans, unterminated


def _strip_comments(text: str) -> str:
    """
    Blank out HTML comments, preserving both offsets and line breaks so
    reported line numbers stay true through a multi-line comment.

    The MSO ghost-table idiom is deliberately unbalanced across two
    comments and is standard production email:

        <!--[if mso]><table><tr><td bgcolor=...><![endif]-->
        <tr><td ...>   the real band   </td></tr>
        <!--[if mso]></td></tr></table><![endif]-->

    Letting those tags into the stack made the ghost <td> the only depth-0
    band and demoted the real one, producing an ERROR-severity false
    positive that fails CI on a correct, industry-standard template. That is
    the outcome this module's own docstring calls fatal.
    """
    spans, _ = _comment_spans(text)
    if not spans:
        return text
    out = list(text)
    for a, b in spans:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


@lru_cache(maxsize=4)
def parse_elements(text: str) -> tuple[Element, ...]:
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
    # Each frame carries the CUMULATIVE inherited ground and cell depth at that
    # point, so neither has to be recomputed by walking the stack on every tag.
    # Recomputing made the walk quadratic in nesting depth - 40,000 unclosed
    # <span>s (240 KB, well inside the size cap) took 17s, and the curve puts a
    # 2 MB file in the tens of minutes. Truncating the stack restores the outer
    # values for free, because each frame stores its own.
    stack: list[tuple[str, str | None, str | None, int]] = []
    out: list[Element] = []
    text = _strip_comments(text)

    for m in TAG_RE.finditer(text):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)

        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
            continue

        style = _style_of(m.group(0))
        whole = m.group(0)
        bad: list[str] = []

        sm = BG_RE.search(style)
        if sm:
            own_bg, bad_bg = _colour_in(sm.group(1))
        else:
            am = BG_ATTR_RE.search(whole)
            own_bg, bad_bg = _colour_in(_attr_value(am)) if am else (None, None)
        if bad_bg:
            bad.append(bad_bg)

        sm = FG_RE.search(style)
        if sm:
            fg, bad_fg = _colour_in(sm.group(1))
        else:
            am = FG_ATTR_RE.search(whole)
            fg, bad_fg = _colour_in(_attr_value(am)) if am else (None, None)
        if bad_fg:
            bad.append(bad_fg)

        inherited = stack[-1][2] if stack else None
        cell_depth = stack[-1][3] if stack else 0

        # An element carrying ONLY an unreadable colour is still recorded, so
        # the run can say so. Dropping it is what made the blindness silent.
        if fg or own_bg or bad:
            out.append(Element(m.start(), name, fg, own_bg or inherited,
                               own_bg, inherited, cell_depth, tuple(bad)))

        if name not in VOID_TAGS and not attrs.rstrip().endswith("/"):
            stack.append((
                name,
                own_bg,
                own_bg or inherited,
                cell_depth + (1 if own_bg and name in ("td", "th") else 0),
            ))

    # A tuple, because this is memoised: returning the list handed every caller
    # the same mutable object, and one append would have poisoned the cache.
    return tuple(out)


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
    # Claiming the check ran is a claim that something was measured. It used to
    # be appended unconditionally, so a template whose every colour was written
    # in a spelling the linter could not read reported `contrast` among the
    # checks run and `clean` as the verdict. Count the comparisons instead.
    measured = 0
    seen_bad: set[str] = set()
    for el in parse_elements(text):
        for raw in el.unresolved:
            if raw in seen_bad:
                continue
            seen_bad.add(raw)
            rep.add(
                "error",
                "unreadable-colour",
                f"`{raw}` is declared as a colour but cannot be read, so nothing here "
                f"was measured against it. Hex must be 3 or 6 digits (8-digit RGBA "
                f"carries an alpha this cannot measure); use a named colour, `rgb()` "
                f"or `hsl()` otherwise.",
                path,
                line_of(text, el.offset),
            )
        if not el.fg or not el.ground:
            continue
        measured += 1
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

    if measured:
        rep.checked.append("contrast")
    else:
        rep.skip("contrast", "no readable text/ground colour pair in this template - "
                             "nothing was measured, so this says nothing about it")


def check_cta(text: str, path: str, cfg: dict, rep: Report) -> None:
    cta = cfg.get("cta") or {}
    fill = _norm_hex(cta.get("fill") or "")
    if not fill.startswith("#"):
        rep.skip("cta-ground", "no mail_design.cta.fill configured")
        return
    rep.checked.append("cta-ground")
    allowed = {_norm_hex(g) for g in (cta.get("allowed_grounds") or [])}
    if not allowed:
        rep.skip("cta-allowlist", "no mail_design.cta.allowed_grounds configured - "
                                  "the measured floor below still applies")
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
        if not ground:
            continue
        ratio = contrast(fill, ground)
        # The MEASURED floor, enforced independently of the allowlist. This was
        # read, interpolated into a message, and never compared to anything -
        # so a button at 2.97:1 against its band passed clean whenever
        # allowed_grounds was empty, while the config comment claimed this was
        # the failure it caught.
        if ratio < non_text:
            rep.add(
                "error",
                "cta-contrast",
                f"The action colour {fill} measures {ratio:.2f}:1 against the ground it "
                f"sits on ({ground}), below the {non_text}:1 non-text minimum. The label "
                f"can be perfect and the button still has no visible edge.",
                path,
                line_of(text, el.offset),
            )
        if allowed and ground not in allowed:
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

    if spec.get("requires_photo"):
        # "Has an <img>" was the first version and it was useless: the ink
        # header strip carries the logo, so EVERY template satisfied it and the
        # check could never fail. A test written for it passed against a
        # template with the photo deleted, which is how it was caught.
        #
        # A photograph in this layout is full-bleed; a logo is not. Width is the
        # discriminator that actually separates them.
        # `\d+` alone read `100` out of `width="100%"` - the spelling every ESP
        # emits for a full-bleed hero - and failed CI on a correct template at
        # ERROR severity. A percentage says nothing about pixels, so fall back
        # to the style width, and when NOTHING resolves to pixels, skip rather
        # than accuse: an unmeasurable template is not a failing one.
        min_w = int(spec.get("photo_min_width") or 400)
        widths: list[int] = []
        percent_only = False
        for tag in re.findall(r"<img\b[^>]*>", text, re.I):
            m = re.search(r"\bwidth\s*:\s*(\d+(?:\.\d+)?)px", tag, re.I)
            if not m:
                m = re.search(r"\bwidth\s*=\s*[\"\']?(\d+(?:\.\d+)?)(?![\d.]*%)", tag, re.I)
            if m:
                widths.append(int(float(m.group(1))))
            elif re.search(r"\bwidth\s*=\s*[\"\']?[\d.]+%", tag, re.I):
                percent_only = True

        if not widths and percent_only:
            rep.skip("family-photo", "every image is sized in percentages, so no pixel "
                                     "width could be resolved - the photograph rule was "
                                     "not applied to this template")
        elif not any(w >= min_w for w in widths):
            rep.add(
                "error",
                "family",
                f"Family `{family}` requires a photograph - an image at least {min_w}px "
                f"wide. Found: {widths or 'no sized images'}. A logo in the header strip "
                "does not satisfy this.",
                path,
                1,
            )

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
    if not (copy.get("forbidden_patterns") or []):
        rep.skip("forbidden-pattern", "no mail_design.copy.forbidden_patterns configured")
    if not (copy.get("forbidden_words") or []):
        rep.skip("forbidden-word", "no mail_design.copy.forbidden_words configured")
    if not (copy.get("placeholder_tokens") or []):
        rep.skip("placeholder", "no mail_design.copy.placeholder_tokens configured")
    if not copy.get("variable_syntax"):
        rep.skip("variables", "no mail_design.copy.variable_syntax configured")

    for entry in copy.get("forbidden_patterns") or []:
        pat, msg = entry.get("pattern"), entry.get("message", "Forbidden pattern.")
        if not pat:
            continue
        rep.checked.append("forbidden-pattern")
        rx = _safe_compile(pat, "copy.forbidden_patterns[].pattern", path, rep)
        if rx is None:
            continue
        hits = _run_guarded(rx, text[:REGEX_INPUT_CAP],
                            "copy.forbidden_patterns[].pattern", path, rep)
        for m in hits:
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
        rx = _safe_compile(syntax, "copy.variable_syntax", path, rep)
        if rx is None:
            return
        # Bound what a capture group can drag into a finding. The default syntax
        # captures a token name; a hostile one captured the whole file.
        raw = _run_guarded(rx, text[:REGEX_INPUT_CAP], "copy.variable_syntax",
                           path, rep, finder="findall")
        used = sorted({(m if isinstance(m, str) else " ".join(m))[:80] for m in raw})
        if used:
            rep.add("info", "variables", f"Declares these variables: {', '.join(used)}. Every one must appear in the template's declared variable list - an undeclared token renders as an empty string, silently.", path, 1)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def lint_file(path: str, cfg: dict, family: str, rep: Report) -> None:
    try:
        size = os.path.getsize(path)
        if size > MAX_TEMPLATE_BYTES:
            rep.add(
                "error",
                "too-large",
                f"{size} bytes exceeds the {MAX_TEMPLATE_BYTES}-byte lint cap and was NOT "
                "checked. A 600px email is a few KB; this is either not a template or it "
                "carries something that does not belong inline.",
                path,
                0,
            )
            return
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        rep.add("error", "unreadable", f"Could not read: {exc}", path, 0)
        return
    # One notion of "what is a comment", applied everywhere. Previously only
    # parse_elements stripped, so markup a client would never execute still
    # raised ERROR-severity findings from the universal checks - the mirror of
    # the suppression bug, and the same root cause: two answers to one question.
    _, unterminated = _comment_spans(text)
    for off in unterminated:
        rep.add(
            "error",
            "unterminated-comment",
            "`<!--` is never closed. A mail client swallows the rest of the document "
            "from here, so nothing below it renders - and the linter will not treat it "
            "as a comment, because doing so would let an unclosed opener blank the "
            "template out of view. Close it.",
            path,
            line_of(text, off),
        )
    text = _strip_comments(text)

    parse_elements.cache_clear()  # per-file memo; see parse_elements
    check_universal(text, path, cfg.get("universal") or {}, rep)
    check_palette(text, path, cfg, rep)
    check_fonts(text, path, cfg, rep)
    check_contrast(text, path, cfg, rep)
    check_cta(text, path, cfg, rep)
    check_family(text, path, cfg, family, rep)
    check_copy(text, path, cfg, rep)


def resolve_targets(args_paths: list[str], cfg: dict, root: str, rep: Report) -> list[str]:
    """
    Resolve the templates to lint, and refuse to leave the repo.

    `mail_design.template_globs` is adopter-supplied config, and config is not
    a trust boundary you get for free. Two things made it one:

    - `os.path.join(root, pattern)` **discards root entirely when pattern is
      absolute**, so "/home/you/.ssh/id_rsa" resolved as directly as the "../"
      form.
    - Findings echo matched substrings, and `copy.variable_syntax` is *also*
      adopter-supplied, so a capture group of `(?s)(.+)` printed whatever the
      first bullet had selected.

    Together that was: config picks any readable file, config-supplied regex
    extracts any part of it, result is printed. A security review demonstrated
    it end to end against /etc/hostname. In CI the yield was low (a read-only,
    job-scoped token), but the skill instructs an agent to run this against
    whatever branch is checked out - so locally it was arbitrary-file-read on
    the operator's machine, triggered by a file in a branch.

    So: resolve every candidate and drop anything outside the root, loudly. A
    rejection is a finding, not a silent skip - a linter that quietly ignores
    what you asked it to check is the failure mode this whole file is against.
    """
    real_root = os.path.realpath(root)

    def contained(p: str) -> bool:
        rp = os.path.realpath(p)
        return rp == real_root or rp.startswith(real_root + os.sep)

    raw: list[str] = []
    if args_paths:
        for p in args_paths:
            raw.extend(sorted(glob.glob(p, recursive=True)) if any(c in p for c in "*?[") else [p])
    else:
        # --require-match only catches TOTAL drift: every glob dead. Adopters
        # accumulate globs over time, so the likelier shape is one live glob and
        # two stale ones - and that exited 0 with nothing said, leaving whole
        # directories silently unlinted. Name the dead ones individually.
        for pattern in cfg.get("template_globs") or []:
            hits = sorted(glob.glob(os.path.join(root, pattern), recursive=True))
            if not hits:
                rep.skip(f"glob:{pattern}", f"template glob `{pattern}` matched no files - "
                                            "nothing under it was checked")
            raw.extend(hits)

    out: list[str] = []
    for p in raw:
        if not os.path.isfile(p):
            continue
        if not contained(p):
            rep.add(
                "error",
                "path-escape",
                f"`{p}` resolves outside {real_root} and was NOT linted. Template globs "
                "may not reach outside the repository.",
                p,
                0,
            )
            continue
        out.append(p)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Lint HTML email templates against the mail design spec.")
    ap.add_argument("paths", nargs="*", help="Template files or globs. Defaults to mail_design.template_globs.")
    ap.add_argument("--family", default="", help="Force a family instead of detecting it from the signature ground.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument(
        "--require-match",
        action="store_true",
        help="Exit non-zero if no template matched. CI passes this: the workflow only "
             "runs when a template path changed, so finding none means the globs and the "
             "workflow's paths filter have drifted apart and the gate is silently absent.",
    )
    args = ap.parse_args(argv)

    root = _ops_root(args.root)
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        # Exit 2, not 1. A broken config is not a lint finding, and a caller
        # that cannot tell "your templates are wrong" from "your config is
        # unreadable" will treat the second as the first.
        print(f"mail-lint: CONFIG ERROR - {exc}", file=sys.stderr)
        return 2
    # B-5: a gate that returns GREEN when it is misconfigured is worse than no
    # gate. Three ways this used to happen, all exit 0:
    #   - `mail_design` absent entirely
    #   - a partial override - {"mail_design": {"palette": ..., "cta": ...}} is
    #     the most natural thing an adopter writes - silently dropping
    #     `template_globs`, because the merge is shallow
    #   - zero templates matched
    # The first two are unambiguously broken config. The third can be
    # legitimate, so it warns by default and is fatal under --require-match,
    # which is what CI passes.
    if not cfg:
        print(
            "mail-lint: CONFIG ERROR - no `mail_design` block found in either "
            "project-config.defaults.json or project-config.json. Refusing to report a "
            "pass for a check that never ran.",
            file=sys.stderr,
        )
        return 2
    if not args.paths and not (cfg.get("template_globs") or []):
        print(
            "mail-lint: CONFIG ERROR - `mail_design.template_globs` is empty or missing, so "
            "no template could ever be found. This is the classic partial-override trap: the "
            "merge is SHALLOW, so defining `mail_design` in project-config.json replaces the "
            "whole subtree and drops any key you did not copy across.",
            file=sys.stderr,
        )
        return 2

    rep = Report()
    targets = resolve_targets(args.paths, cfg, root, rep)
    if not targets and not rep.findings:
        msg = ("mail-lint: no templates matched " + (", ".join(args.paths) if args.paths
               else ", ".join(cfg.get("template_globs") or [])) +
               " - NOTHING WAS CHECKED.")
        print(msg, file=sys.stderr)
        if args.require_match:
            print("mail-lint: --require-match was set, so this is a failure.", file=sys.stderr)
            return 2
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
