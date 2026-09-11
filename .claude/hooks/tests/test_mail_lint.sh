#!/bin/bash
# Tests for mail-lint.py — the deterministic email-template linter.
#
# The linter's whole justification is that it is RELIABLE where a model is not,
# so these tests are mostly negative: each one plants a specific defect and
# asserts the linter names it. A linter that only ever passes is indistinguishable
# from one that does nothing.
#
# Run: bash .claude/hooks/tests/test_mail_lint.sh

set -u

HOOK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LINT="$HOOK_DIR/mail-lint.py"
PASS=0
FAIL=0

FIX=$(mktemp -d)
trap 'rm -rf "$FIX"' EXIT
mkdir -p "$FIX/.claude" "$FIX/emails"
: > "$FIX/.apexyard-fork"

cat > "$FIX/.claude/project-config.json" <<'JSON'
{
  "mail_design": {
    "template_globs": ["emails/**/*.html"],
    "universal": { "expected_width_px": 600 },
    "palette": { "ink": "#131110", "card": "#FFFDF9", "lime": "#E3FF6B", "orange": "#F65802" },
    "fonts": { "stacks": [{ "face": "Playfair Display", "requires_fallback": ["Georgia"] }] },
    "families": {
      "marketing": { "signature_ground": "#E3FF6B", "max_signature_bands": 1, "requires_photo": true, "allows_unsubscribe": true },
      "transactional": { "signature_ground": "#E3DBED", "max_signature_bands": 1, "allows_unsubscribe": false }
    },
    "cta": { "fill": "#F65802", "allowed_grounds": ["#FFFDF9"], "max_per_email": 1 },
    "contrast": { "text_min": 4.5, "non_text_min": 3.0,
                  "accepted_exceptions": [{ "fg": "#FFFDF9", "bg": "#F65802" }] },
    "copy": { "forbidden_patterns": [{ "pattern": "—", "message": "em dash" }],
              "placeholder_tokens": ["PHOTO_URL"] }
  }
}
JSON

# A clean marketing template: ink strip, photo, lime band, white body + orange
# CTA. Every case below is this file with one thing broken.
write_template() {
  cat > "$FIX/emails/t.html" <<HTML
<table cellpadding="0" cellspacing="0" width="600" style="width:600px;">
<tr><td style="background-color:#131110;padding:16px;">
  <img src="https://x.test/logo.png" alt="Salto" width="88" />
</td></tr>
<tr><td style="font-size:0;"><img src="https://x.test/p.jpg" alt="" width="600" /></td></tr>
<tr><td style="background-color:#E3FF6B;padding:32px;">
  <p style='font-family:"Playfair Display", Georgia, serif;color:#131110;'>Headline</p>
</td></tr>
<tr><td style="background-color:#FFFDF9;padding:32px;">
  <p style="color:#131110;">Body</p>
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="background-color:#F65802;border-radius:4px;">
      <a href="https://x.test" style="color:#FFFDF9;">Go</a>
    </td></tr></table>
</td></tr>
${EXTRA:-}
</table>
HTML
}

run() { python3 "$LINT" --root "$FIX" ${RUN_ARGS:-} 2>&1; }

check() { # name, expect-substring ("" = expect clean)
  local name="$1" expect="$2" out
  out=$(run)
  if [ -z "$expect" ]; then
    if echo "$out" | grep -q '^clean$'; then
      echo "  PASS: $name"; PASS=$((PASS + 1))
    else
      echo "  FAIL: $name — expected clean, got:"; echo "$out" | sed 's/^/        /'; FAIL=$((FAIL + 1))
    fi
  elif echo "$out" | grep -qi -- "$expect"; then
    echo "  PASS: $name"; PASS=$((PASS + 1))
  else
    echo "  FAIL: $name — expected /$expect/, got:"; echo "$out" | sed 's/^/        /'; FAIL=$((FAIL + 1))
  fi
}

echo "mail-lint tests"

EXTRA="" write_template
check "a spec-compliant template lints clean" ""

EXTRA="" write_template
sed -i 's/color:#131110;">Body/color:rgba(19,17,16,0.7);">Body/' "$FIX/emails/t.html"
check "rejects rgba()" "alpha-colour"

# The realistic shape of the bug: a SINGLE-quoted style attribute carrying a
# DOUBLE-quoted font name. That is how the design package's own font token is
# spelled, so it is the form an author most easily reaches for - and it is
# invisible to any regex whose character class excludes a quote.
EXTRA="" write_template
sed -i 's/, Georgia, serif;color/;color/' "$FIX/emails/t.html"
check "catches a brand face with no fallback, double-quoted in a single-quoted attr" "font-fallback"

EXTRA="" write_template
sed -i 's/alt="" width="600"/width="600"/' "$FIX/emails/t.html"
check "requires alt on every image" "img-attrs"

EXTRA="" write_template
sed -i 's/background-color:#FFFDF9;padding:32px;/background-color:#E3FF6B;padding:32px;/' "$FIX/emails/t.html"
check "rejects the orange CTA on a lime band" "cta-ground"

EXTRA="" write_template
sed -i 's/<p style="color:#131110;">Body/<p style="color:#DCDAD6;">Body/' "$FIX/emails/t.html"
check "computes contrast and fails a too-light body" "contrast"

EXTRA='<tr><td style="background-color:#E3FF6B;padding:32px;"><p style="color:#131110;">Second</p></td></tr>' write_template
check "rejects a second signature band" "signature bands"

EXTRA="" write_template
python3 -c "import io;p='$FIX/emails/t.html';s=open(p,encoding='utf-8').read();open(p,'w',encoding='utf-8').write(s.replace('>Body<','>Body \u2014 padded<'))"
check "rejects an em dash" "forbidden-pattern"

EXTRA="" write_template
sed -i 's|https://x.test/p.jpg|PHOTO_URL|' "$FIX/emails/t.html"
check "rejects a leftover placeholder" "placeholder"

EXTRA="" write_template
sed -i 's|<a href="https://x.test"|<a href="https://x.test/unsubscribe"|' "$FIX/emails/t.html"
python3 "$LINT" --root "$FIX" --family transactional 2>&1 | grep -qi "unsubscribe" \
  && { echo "  PASS: rejects unsubscribe in a family that forbids it"; PASS=$((PASS + 1)); } \
  || { echo "  FAIL: rejects unsubscribe in a family that forbids it"; FAIL=$((FAIL + 1)); }

# The button's white label sits on ORANGE, not on the white band that contains
# it. A scan that finds "the enclosing cell" rather than the nearest
# background-carrying ancestor reports 1:1 here and is useless. This is the
# regression that caught the first draft.
EXTRA="" write_template
check "attributes the button label to the button, not the band it sits in" ""

# ---------------------------------------------------------------------------
# One negative case per remaining check.
#
# A review mutation-tested all 23 checks - neuter the check, run the suite,
# expect red - and found 12 SURVIVED. Eight of ten universal rules, the ones ON
# by default for every adopter, were unconstrained, as was palette detection.
# For a linter whose entire claim is reliability, a check with no test is a
# check nobody has evidence works.
# ---------------------------------------------------------------------------

EXTRA="" write_template
sed -i 's|<table cellpadding="0" cellspacing="0" width="600"|<style>.x{color:red}</style><table cellpadding="0" cellspacing="0" width="600"|' "$FIX/emails/t.html"
check "rejects a <style> block carrying layout" "style-block"

EXTRA="" write_template
sed -i 's|<table cellpadding="0" cellspacing="0" width="600"|<link href="https://fonts.googleapis.com/css2?family=X" rel="stylesheet"><table cellpadding="0" cellspacing="0" width="600"|' "$FIX/emails/t.html"
check "rejects a third-party font host" "font-privacy"

EXTRA="" write_template
sed -i 's|<p style="color:#131110;">Body|<script>alert(1)</script><p style="color:#131110;">Body|' "$FIX/emails/t.html"
check "rejects a script tag" "script"

EXTRA="" write_template
sed -i 's|alt="Salto" width="88"|alt="Salto" width="88" style="filter:brightness(0) invert(1);"|' "$FIX/emails/t.html"
check "rejects a CSS filter - the logo-knockout trick that fails in many clients" "css-filter"

EXTRA="" write_template
sed -i 's|<img src="https://x.test/p.jpg" alt="" width="600" />|<img src="https://x.test/p.jpg" alt="" />|' "$FIX/emails/t.html"
check "requires an explicit width on every image" "img-attrs"

EXTRA="" write_template
sed -i 's|<p style="color:#131110;">Body|<div style="display:flex;">flex</div><p style="color:#131110;">Body|' "$FIX/emails/t.html"
check "rejects modern layout Outlook cannot render" "modern-layout"

EXTRA="" write_template
sed -i 's/600/640/g' "$FIX/emails/t.html"
check "warns when the fixed column width is absent" "column-width"

EXTRA="" write_template
sed -i 's|color:#131110;">Body|color:#AABBCC;">Body|' "$FIX/emails/t.html"
check "rejects a hex outside the palette" "palette"

EXTRA="" write_template
python3 - "$FIX/emails/t.html" <<'PY2'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
# a second CTA in the same body band
s = s.replace('</td></tr></table>\n</td></tr>',
              '</td></tr></table>\n  <table cellpadding="0" cellspacing="0"><tr>'
              '<td style="background-color:#F65802;border-radius:4px;">'
              '<a href="https://x.test" style="color:#FFFDF9;">Two</a></td></tr></table>\n</td></tr>', 1)
open(p, "w", encoding="utf-8").write(s)
PY2
check "warns on more than one call to action" "cta-count"

EXTRA="" write_template
sed -i '/<img src="https:\/\/x.test\/p.jpg"/d' "$FIX/emails/t.html"
check "requires a photograph in a family that mandates one" "photograph"

EXTRA='<tr><td style="background-color:#FFFDF9;padding:32px;"><p style="color:#131110;">Adjacent</p></td></tr>' write_template
check "warns on two adjacent bands sharing a ground" "band-sequence"

# --- checks added in response to the review -------------------------------

EXTRA="" write_template
python3 - "$FIX/emails/t.html" <<'PY2'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
# bgcolor is the attribute Outlook honours; reading only the CSS form made the
# linter blind on the most common authoring style.
s = s.replace('style="background-color:#E3FF6B;padding:32px;"', 'bgcolor="#E3FF6B" style="padding:32px;"')
s = s.replace('color:#131110;\'>Headline', "color:#DCDAD6;'>Headline")
open(p, "w", encoding="utf-8").write(s)
PY2
check "sees a bgcolor ground and measures contrast against it" "contrast"

EXTRA="" write_template
sed -i 's|color:#131110;">Body|color:#ddd;">Body|' "$FIX/emails/t.html"
check "sees three-digit hex" "palette"

# The MSO ghost-table idiom is deliberately unbalanced across two comments and
# is standard production email. Letting it into the tag stack demoted the real
# band and produced an ERROR on a CORRECT template.
EXTRA="" write_template
python3 - "$FIX/emails/t.html" <<'PY2'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = s.replace('<tr><td style="background-color:#E3FF6B;padding:32px;">',
  '<!--[if mso]><table><tr><td bgcolor="#131110"><![endif]-->\n'
  '<tr><td style="background-color:#E3FF6B;padding:32px;">', 1)
s = s.replace('</td></tr>\n<tr><td style="background-color:#FFFDF9;',
  '</td></tr>\n<!--[if mso]></td></tr></table><![endif]-->\n'
  '<tr><td style="background-color:#FFFDF9;', 1)
open(p, "w", encoding="utf-8").write(s)
PY2
check "is not fooled by MSO conditional comments" ""

# The mirror of the MSO case, and the reason it is dangerous. `<!--` inside a
# QUOTED ATTRIBUTE VALUE is literal text to every mail client, but a raw
# `<!--.*?-->` strip reads it as a comment opener and blanks the markup
# between. That hands an author a way to silence a finding instead of fixing
# it - a 1.12:1 body shipping with the gate green. Both halves matter: the
# violation must still be named, and the fake markers must not move it.
EXTRA='<tr><td bgcolor="#FFFDF9"><span title="<!--"></span><font color="#FFFDF9">faint</font><span title="-->"></span></td></tr>' write_template
check "a fake comment in an attribute value cannot hide a violation" "contrast"

# The same question answered consistently in the other direction: markup a
# client will never execute must not raise findings either. Before the scan was
# shared, parse_elements stripped and the universal checks did not, so dead
# markup produced ERROR-severity false positives.
EXTRA='<!-- superseded: <div style="display:flex"><script>x</script></div> -->' write_template
check "markup inside a real comment raises nothing" ""

# An unterminated opener is NOT treated as a comment. Honouring it would
# swallow the rest of the document and hand back the same suppression
# primitive through a shorter door, so it is reported instead.
EXTRA='<!-- opened and never closed' write_template
check "an unterminated comment is reported, not obeyed" "unterminated-comment"

# --- the template must not be able to silence the check on itself ---------
# Three more comment-suppression spellings, each found by attacking the
# tag-aware scanner that was written to close the first one. In every case a
# real parser resumes rendering where the linter kept swallowing.

# `--!>` closes a comment (WHATWG 13.2.5.52, comment-end-bang state).
EXTRA='<tr><td bgcolor="#FFFDF9"><font color="#FFFDF9">faint</font></td></tr>' write_template
sed -i '1i <!-- harmless --!>' "$FIX/emails/t.html"
check "a --!> terminator cannot hide a violation" "contrast"

# `<!--` inside RCDATA/RAWTEXT is literal text, not a comment opener.
for el in textarea title style; do
  EXTRA="<tr><td bgcolor=\"#FFFDF9\"><font color=\"#FFFDF9\">faint</font></td></tr>" write_template
  sed -i "1i <$el><!--</$el>" "$FIX/emails/t.html"
  check "a <$el> cannot be used to open a fake comment" "contrast"
done

# ...and the opposite direction: <!--> is a COMPLETE empty comment in HTML5,
# so reporting it as unterminated was a false positive.
EXTRA='<!-->' write_template
check "an empty <!--> comment is not reported as unterminated" ""

# --- a colour must never be invented from a filename ----------------------
# `_COLOUR_TOKEN`'s bare-word arm read inside url(), so any path segment that
# happened to be a CSS colour name became the resolved ground. Renaming a hero
# image was enough to silence a real contrast error - no adversary required.
EXTRA='<tr><td style="background: url(assets/navy-hero.png);"><font color="#FFFDF9">faint</font></td></tr>' write_template
check "does not read a colour out of a url() path" "unreadable-colour"

# The same rule must not turn every decorative background into an error.
EXTRA='<tr><td style="background: url(assets/navy-hero.png);"></td></tr>' write_template
check "an image ground with no text over it raises nothing" ""

# A button on an image ground has no measurable edge - say so rather than
# measuring against a colour it does not sit on.
EXTRA="" write_template
sed -i 's|<tr><td style="background-color:#FFFDF9;padding:32px;">|<tr><td style="background: url(band.png);padding:32px;">|' "$FIX/emails/t.html"
check "reports a CTA sitting on an image ground" "background IMAGE"

# A gradient is no more measurable than a photograph - resolving it to its
# first stop would be a guess reported as a measurement.
EXTRA='<tr><td style="background: linear-gradient(to right, #FFFDF9, #131110);"><font color="#FFFDF9">faint</font></td></tr>' write_template
check "does not measure against a gradient's first stop" "unreadable-colour"

# An explicit colour alongside a url() is still found.
EXTRA='<tr><td style="background: url(hero.png) #FFFDF9;"><font color="#FFFDF9">faint</font></td></tr>' write_template
check "finds the colour when a url() precedes it" "contrast"

# --- inputs that used to escape the handlers entirely ---------------------
EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "a{1,4294967296}", "message": "x"}]
json.dump(d, open(sys.argv[1], "w"))
PY2
check "a repetition count too large is a config defect, not a crash" "config"
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "—", "message": "em dash"}]
json.dump(d, open(sys.argv[1], "w"))
PY2

# A non-finite component must not resolve to a real-looking colour that then
# gets reported as a MEASURED ratio.
out=$(python3 - "$HOOK_DIR/mail-lint.py" <<'PY2'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ml", sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules["ml"] = m; spec.loader.exec_module(m)
bad = [v for v in ("rgb(inf,0,0)", "rgb(1e400,0,0)", "hsl(nan,50%,50%)", "hsl(inf,50%,50%)")
       if m._resolve_colour(v) is not None]
print("LEAKED:" + ",".join(bad) if bad else "ALLNONE")
print("SANE" if m._resolve_colour("hsl(120,100%,25%)") == "#008000" else "BROKEN")
PY2
)
if [ "$(printf '%s' "$out" | head -1)" = "ALLNONE" ] && [ "$(printf '%s' "$out" | tail -1)" = "SANE" ]; then
  echo "  PASS: a non-finite colour component resolves to nothing, not a number"; PASS=$((PASS + 1))
else
  echo "  FAIL: a non-finite colour component resolves to nothing, not a number — $out"; FAIL=$((FAIL + 1))
fi

# Untrusted config reaching the CI stream must not be able to start a line at
# column 0, where GitHub Actions parses ::workflow-commands::.
EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["template_globs"] = ["emails/**/*.html", "dead/\n::error::INJECTED\nx/*.html"]
json.dump(d, open(sys.argv[1], "w"))
PY2
if [ "$(run | grep -c '^::')" = "0" ]; then
  echo "  PASS: an injected workflow command cannot reach column 0 of stdout"; PASS=$((PASS + 1))
else
  echo "  FAIL: an injected workflow command cannot reach column 0 of stdout"; FAIL=$((FAIL + 1))
fi
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["template_globs"] = ["emails/**/*.html"]
json.dump(d, open(sys.argv[1], "w"))
PY2

# --- colour grammar -------------------------------------------------------
# The contrast layer used to read hex and nothing else, so the most common
# ground spelling in hand-written email was invisible and a 1.03:1 body
# reported clean - while the report still listed `contrast` among checks run.
EXTRA='<tr><td bgcolor="white"><p style="color:#FFFDF9;">1.03:1</p></td></tr>' write_template
check "measures contrast against a named-colour ground" "contrast"

EXTRA='<tr><td style="background-color:rgb(255,255,255)"><p style="color:#FFFDF9;">1.03:1</p></td></tr>' write_template
check "measures contrast against an rgb() ground" "contrast"

EXTRA='<tr><td style="background-color:hsl(0,0%,100%)"><p style="color:#FFFDF9;">1.03:1</p></td></tr>' write_template
check "measures contrast against an hsl() ground" "contrast"

EXTRA='<tr><td style="background: url(hero.png) #FFFDF9"><p style="color:#FFFDF9;">1:1</p></td></tr>' write_template
check "finds the colour when it is not first in the declaration" "contrast"

# A malformed hex must be REFUSED, never normalised into a colour that appears
# nowhere. `#12345` used to become `#112233` and get reported as a measured
# ratio against a real line number.
EXTRA='<tr><td bgcolor="#12345"><p style="color:#131110;">x</p></td></tr>' write_template
check "refuses a malformed hex instead of inventing a colour" "unreadable-colour"

EXTRA='<tr><td bgcolor="#ff0000ff"><p style="color:#131110;">x</p></td></tr>' write_template
check "refuses 8-digit RGBA rather than dropping its alpha" "unreadable-colour"

# --- the photograph rule --------------------------------------------------
# width="100%" is what every ESP emits for a full-bleed hero. Reading `100` out
# of it failed CI on a correct template at ERROR severity.
EXTRA="" write_template
sed -i 's|<img src="https://x.test/p.jpg" alt="" width="600" />|<img src="https://x.test/p.jpg" alt="" width="100%" style="width:600px" />|' "$FIX/emails/t.html"
check "accepts a full-bleed hero sized in percent with a pixel style" ""

# ...and the rule must still bite when there is genuinely no photograph.
EXTRA="" write_template
sed -i 's|<tr><td style="font-size:0;"><img src="https://x.test/p.jpg" alt="" width="600" /></td></tr>||' "$FIX/emails/t.html"
check "still requires a photograph when none is present" "requires a photograph"

# --- sites that survived mutation ----------------------------------------
# Each of these was deletable with the suite still green, and each is the
# direct subject of a fix claimed in this PR.
EXTRA="" write_template
sed -i 's/background-color:#E3FF6B;padding:32px;/background-color:#FFFDF9;padding:32px;/' "$FIX/emails/t.html"
RUN_ARGS="--family marketing" check "reports a family with no signature band" "No signature band found"

EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["cta"]["allowed_grounds"] = ["#FFFDF9", "#EFEDE5"]
json.dump(d, open(sys.argv[1], "w"))
PY2
sed -i 's/background-color:#FFFDF9;padding:32px;/background-color:#EFEDE5;padding:32px;/' "$FIX/emails/t.html"
check "measures the CTA against its ground, not just the allowlist" "cta-contrast"
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["cta"]["allowed_grounds"] = ["#FFFDF9"]
json.dump(d, open(sys.argv[1], "w"))
PY2

cat > "$FIX/emails/t.html" <<'HTML'
<div style="width:600px;background-color:#FFFDF9;">
  <p style="color:#131110;">A div-only layout Outlook will not render.</p>
</div>
HTML
check "rejects a layout that is not table-based" "table-layout"

# The universal skip loop IS the fix for "checks that did not run said nothing".
# Deleting it left the suite green, so it is pinned here.
EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["universal"]["forbid_script"] = False
json.dump(d, open(sys.argv[1], "w"))
PY2
check "a disabled universal rule is recorded as a skip, not a pass" "universal.forbid_script is disabled"
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["universal"]["forbid_script"] = True
json.dump(d, open(sys.argv[1], "w"))
PY2

EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["forbidden_words"] = ["synergy"]
json.dump(d, open(sys.argv[1], "w"))
PY2
EXTRA='<tr><td><p style="color:#131110;">Real synergy here</p></td></tr>' write_template
check "names a forbidden word" "forbidden-word"

EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["variable_syntax"] = r"\{\{\s*(\w+)\s*\}\}"
del d["mail_design"]["copy"]["forbidden_words"]
json.dump(d, open(sys.argv[1], "w"))
PY2
EXTRA='<tr><td><p style="color:#131110;">Hello {{ first_name }}</p></td></tr>' write_template
check "inventories the template variables" "first_name"

# The ReDoS ceiling, pinned cheaply via the env override rather than by paying
# the full 5s. The guard is what stands between an adopter config and a hung job.
EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "(a+)+$", "message": "redos"}]
json.dump(d, open(sys.argv[1], "w"))
PY2
sed -i 's|<p style="color:#131110;">Body</p>|<p style="color:#131110;">aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab</p>|' "$FIX/emails/t.html"
out=$(MAIL_LINT_REGEX_TIMEOUT=1 run)
if echo "$out" | grep -qi "did not finish within"; then
  echo "  PASS: a catastrophically backtracking config regex is abandoned, not run forever"; PASS=$((PASS + 1))
else
  echo "  FAIL: a catastrophically backtracking config regex is abandoned, not run forever"; FAIL=$((FAIL + 1))
fi
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "—", "message": "em dash"}]
del d["mail_design"]["copy"]["variable_syntax"]
json.dump(d, open(sys.argv[1], "w"))
PY2

EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "([unclosed", "message": "x"}]
json.dump(d, open(p, "w"))
PY2
check "reports a malformed config regex as a CONFIG defect, not a crash" "not a valid regular expression"

# Restore a sane config for the remaining cases.
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mail_design"]["copy"]["forbidden_patterns"] = [{"pattern": "\u2014", "message": "em dash"}]
json.dump(d, open(p, "w"))
PY2

# --- sites that survived the second mutation sweep ------------------------
EXTRA='<tr><td><style>.x{color:red}</style></td></tr>' write_template
sed -i 's|</style>||' "$FIX/emails/t.html"
check "reports a <style> tag that is never closed" "style-block"

EXTRA='<tr><td><style>@import url("local.css");</style></td></tr>' write_template
check "rejects @import" "font-privacy"

EXTRA='<tr><td style="font-family:X"><p style="color:#131110;">see fonts.gstatic.com</p></td></tr>' write_template
check "names a third-party font host even outside a link" "font-privacy"

# Family detection has two distinct give-up paths and both were deletable.
cat > "$FIX/emails/t.html" <<'HTML'
<table width="600" style="width:600px;"><tr><td bgcolor="#FFFDF9">
  <p style="color:#131110;">No signature ground anywhere.</p>
</td></tr></table>
HTML
check "says so when it cannot tell which family a template is" "Could not tell which family"

EXTRA="" write_template
RUN_ARGS="--family nosuchfamily" check "rejects a family name that is not configured" "Unknown family"

# Resource guards. Both are the linter refusing to pretend it checked something.
EXTRA="" write_template
python3 -c "open('$FIX/emails/t.html','w').write('<table>' + 'x'*3000000 + '</table>')"
check "refuses a template past the size cap instead of checking it partly" "too-large"

printf '<table><tr><td>caf\xe9</td></tr></table>' > "$FIX/emails/t.html"
check "reports a template that is not UTF-8 rather than crashing the run" "unreadable"

# Every skip is a promise that the run says nothing about that check. Ten of
# eleven skip sites were deletable with the suite still green, which made the
# skip-loudly contract itself unverified - the same posture that produced the
# original finding.
cat > "$FIX/.claude/project-config.json" <<'JSON4'
{ "mail_design": { "template_globs": ["emails/**/*.html"],
                   "families": { "marketing": { "signature_ground": "#E3FF6B", "requires_photo": true } } } }
JSON4
cat > "$FIX/emails/t.html" <<'HTML'
<table><tr><td bgcolor="#E3FF6B"><img src="h.jpg" alt="" width="100%" /></td></tr></table>
HTML
out=$(run)
missing=""
for want in "column-width" "font-fallback" "cta-ground" "forbidden-pattern" \
            "forbidden-word" "placeholder" "variables" "family-photo"; do
  echo "$out" | grep -q -- "$want" || missing="$missing $want"
done
if [ -z "$missing" ]; then
  echo "  PASS: every unconfigured check names itself in SKIPPED"; PASS=$((PASS + 1))
else
  echo "  FAIL: every unconfigured check names itself in SKIPPED — missing:$missing"; FAIL=$((FAIL + 1))
fi

# A template with no readable colour pair must SKIP contrast, not claim it ran.
cat > "$FIX/emails/t.html" <<'HTML'
<table><tr><td><p>No colour is declared anywhere in this template.</p></td></tr></table>
HTML
check "skips contrast when nothing measurable was found" "nothing was measured"

# An unconfigured family set must say so. Without a test the skip was
# deletable, which would leave a run silently saying nothing about families.
cat > "$FIX/.claude/project-config.json" <<'JSON6'
{ "mail_design": { "template_globs": ["emails/**/*.html"] } }
JSON6
cat > "$FIX/emails/t.html" <<'HTML'
<table><tr><td bgcolor="#FFFDF9"><p style="color:#131110;">Body</p></td></tr></table>
HTML
check "says so when no families are configured" "no mail_design.families configured"

# Partial glob drift: one live glob, one stale. This exited 0 saying nothing,
# so a whole directory could go unlinted without a word.
cat > "$FIX/.claude/project-config.json" <<'JSON7'
{ "mail_design": { "template_globs": ["emails/**/*.html", "newsletters/**/*.html"] } }
JSON7
check "names a template glob that matched nothing" "matched no files"

# cta.fill set but no allowlist: the allowlist skips, the measured floor stays.
cat > "$FIX/.claude/project-config.json" <<'JSON5'
{ "mail_design": { "template_globs": ["emails/**/*.html"],
                   "cta": { "fill": "#F65802" } } }
JSON5
cat > "$FIX/emails/t.html" <<'HTML'
<table><tr><td bgcolor="#FFFDF9"><table><tr><td bgcolor="#F65802">
<a href="#" style="color:#FFFDF9;">Go</a></td></tr></table></td></tr></table>
HTML
check "skips the CTA allowlist but keeps the measured floor" "cta-allowlist"

# A gate that returns green when misconfigured is worse than no gate.
EXTRA="" write_template
python3 - "$FIX/.claude/project-config.json" <<'PY2'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
del d["mail_design"]["template_globs"]      # the classic partial-override trap
json.dump(d, open(p, "w"))
PY2
python3 "$LINT" --root "$FIX" >/dev/null 2>&1
if [ $? -eq 2 ]; then
  echo "  PASS: fails closed when a partial override drops template_globs"; PASS=$((PASS + 1))
else
  echo "  FAIL: fails closed when a partial override drops template_globs"; FAIL=$((FAIL + 1))
fi

# A config you cannot parse must never be replaced with looser defaults.
printf '{"mail_design":{"palette":{"ink":"#131110"' > "$FIX/.claude/project-config.json"
python3 "$LINT" --root "$FIX" >/dev/null 2>&1
if [ $? -eq 2 ]; then
  echo "  PASS: fails closed on an unparseable override config"; PASS=$((PASS + 1))
else
  echo "  FAIL: fails closed on an unparseable override config"; FAIL=$((FAIL + 1))
fi

# The two cases above deliberately leave the config broken. Rewrite a valid one
# before the final case, which needs a parseable config to test skipping.
cat > "$FIX/.claude/project-config.json" <<'JSON2'
{ "mail_design": { "template_globs": ["emails/**/*.html"],
                   "palette": { "ink": "#131110" },
                   "cta": { "fill": "#F65802", "allowed_grounds": ["#FFFDF9"] } } }
JSON2

# A check whose config is empty must SKIP LOUDLY, never pass silently.
python3 - <<'PY' "$FIX"
import json, sys
p = sys.argv[1] + "/.claude/project-config.json"
d = json.load(open(p))
d["mail_design"]["palette"] = {}
d["mail_design"]["cta"]["fill"] = ""
json.dump(d, open(p, "w"))
PY
out=$(run)
if echo "$out" | grep -q "SKIPPED" && echo "$out" | grep -q "no mail_design.palette configured"; then
  echo "  PASS: an unconfigured check skips loudly instead of passing silently"; PASS=$((PASS + 1))
else
  echo "  FAIL: an unconfigured check skips loudly instead of passing silently"; FAIL=$((FAIL + 1))
fi

# Path containment had no test, which made it the finding most likely to be
# quietly undone by a future refactor of resolve_targets.
cat > "$FIX/.claude/project-config.json" <<'JSON3'
{ "mail_design": { "template_globs": ["../outside/*.html"] } }
JSON3
mkdir -p "$(dirname "$FIX")/outside"
: > "$(dirname "$FIX")/outside/leak.html"
check "a glob escaping the configured root is refused" "path-escape"

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
