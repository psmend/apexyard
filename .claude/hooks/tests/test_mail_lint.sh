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

run() { python3 "$LINT" --root "$FIX" 2>&1; }

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

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
