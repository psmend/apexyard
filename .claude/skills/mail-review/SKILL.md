---
name: mail-review
description: Audit HTML email templates — deterministic lint (contrast, palette, fonts, layout, family rules) plus Barid's judgement pass on register, copy and consent. The email analog of /code-review.
---

# /mail-review — Audit email templates

Email is the surface most likely to drift, and the least likely to be caught:
its content often lives in a database column or a third-party editor rather
than in the repo, there is no build step, and nothing reviews it on the way in.
This skill is the review that surface never had.

## Usage

```
/mail-review                          # every template in mail_design.template_globs
/mail-review emails/welcome.html      # one file
/mail-review 'emails/**/*.html'       # a glob
/mail-review --family transactional   # force the family instead of detecting it
/mail-review --lint-only              # skip the judgement pass
```

## The two passes, and why they are separate

| Pass | Does | Decides |
|---|---|---|
| **1. Lint** — `.claude/hooks/mail-lint.py` | Contrast (computed), palette, font fallbacks, layout, images, family rules, forbidden copy patterns, placeholders | Mechanically. Same answer every time |
| **2. Judge** — Barid, `.claude/agents/mail-reviewer.md` | Headline register, padded copy, images-off readability, dark-mode risk, **and whether a clinical message is wearing a marketing template** | By judgement, against the project's own spec |

The split is the point. A model asked to check thirty mechanical rules across
twenty templates will get most of them right — which is the worst available
outcome, because nobody can tell which ones. So a formula owns the decidable
half and the agent owns the rest, with the lint output already in its brief so
it never re-derives what a formula settles. Rex uses the same shape with its
static-analysis pass.

## Process

### 1. Lint

```bash
python3 .claude/hooks/mail-lint.py [paths...] [--family <name>] --json
```

Exit **0** clean, **1** findings at `error` severity, **2** the config itself is
broken (unparseable override, a top level that is not a JSON object, missing
`mail_design`, empty `template_globs`, or `--require-match` with nothing
matched). Exit 2 is deliberately distinct: "your config is wrong" must never
look like "your templates are wrong."

**On exit 2 `--json` writes nothing to stdout** — the error goes to stderr. A
consumer that pipes stdout into a JSON parser gets a parse error, not an error
object, so check the exit code before parsing. The CI template does exactly
that.

One policy decision worth knowing, because it cuts both ways: **HTML comments
are stripped before every check**, using a tag-aware scan rather than a
`<!--.*?-->` search. So commented-out markup raises nothing, and a `<!--` inside
a quoted attribute value — literal text to every mail client — cannot be used to
blank a violation out of view. An unterminated `<!--` is reported rather than
obeyed. The cost is that content inside an MSO conditional comment, which
Outlook *does* render, is not linted; that is the deliberate trade recorded in
AgDR-0123.

Read the `skipped` array in the
output and **report it to the operator verbatim** — a check that did not run
because its config is empty says nothing about the template, and silence there
is how a linter becomes a liability.

If `mail_design.palette` and friends are unconfigured, say so and point at
`.claude/project-config.json`. The universal rules still ran; the brand rules
did not.

### 2. Judge

Stop here if `--lint-only`. Otherwise spawn Barid via the `Agent` tool with
`subagent_type: mail-reviewer`, and put **the lint output in the brief** along
with the template paths and the path to the project's mail spec
(`mail_design.spec_path`).

Barid is advisory. It writes nothing and holds no marker.

### 3. Report

Lead with Barid's family/consent finding if it fired — it is the one with a
silent failure mode. Then the lint errors, then the judgement findings, then
what did not run.

## The gate

**The lint gates via CI. The judgement never gates.**

There is **no `require-mail-lint.sh`, and no PreToolUse hook**. An earlier draft
of this file said there was — while the AgDR in the same PR argued at length for
the opposite — and a reviewer caught it. Worth leaving the correction visible:
believing a gate protects you when it does not is the failure `.claude/rules/`
spends most of its length preventing.

What actually happens:

1. `golden-paths/pipelines/mail-lint.yml` runs the linter on every PR touching a
   template. **It is a template you must copy to `.github/workflows/`** — it is
   not active anywhere until an adopter installs it.
2. A lint error fails that job.
3. `block-merge-on-red-ci.sh` — which already exists, already covers both merge
   shapes — refuses the merge while CI is red.

So the lint is a merge gate *one step earlier* than a hook would be, and unlike
a hook it also catches a human merging in the GitHub UI. Full reasoning and the
rejected alternatives: **AgDR-0123**.

Barid's verdict blocks nothing, ever. Lint findings are decidable, so blocking
on them is fair; blocking on a model's opinion about whether a headline sounds
like the brand generates override pressure, and a gate that is routinely
overridden stops working for the cases that matter.

One consequence worth stating precisely, because the strong version of it is
wrong: there is **no approval marker on this surface**, so the
build-agent-forges-its-own-approval failure mode cannot occur here. But the
trust does not vanish — it **moves to the workflow file and to
`project-config.json`**. Whoever can edit those can weaken the gate. That is the
same exposure every CI-based check carries, and it is why the linter now refuses
to report a pass when its own config is missing or unparseable.

## Configuration

Everything brand-specific lives in `.claude/project-config.defaults.json` →
`mail_design`, overridable in `.claude/project-config.json`. Two tiers:

- **`universal`** — true of HTML email regardless of brand, **on by default**.
  A fresh adopter gets value with zero configuration.
- **Brand rules** — `palette`, `fonts.stacks`, `families`, `cta`,
  `copy.forbidden_*` — ship **empty**. The checks that depend on them SKIP and
  say so. The linter never guesses a house style.

Point `spec_path` at your own mail spec. Barid reviews against *your* rules; a
review with no standard behind it is an opinion, and Barid will label it one.

## When NOT to use this

- **You want a WCAG sweep of a web page** — that is `/accessibility-audit`.
  Email's constraints are different enough (no zoom, no focus states, blocked
  images, client-imposed dark mode) to need their own pass, but a landing page
  is not this skill's job.
- **You are designing the templates, not auditing them** — this reviews what
  exists.
- **There is no mail spec and nobody has ruled one** — run it anyway for the
  universal rules, but expect the brand half to skip. Write the spec first;
  `/write-spec` or your design owner.

## Related

| | |
|---|---|
| `.claude/agents/mail-reviewer.md` | Barid, the judgement pass |
| `.claude/hooks/mail-lint.py` | The deterministic pass |
| `golden-paths/pipelines/mail-lint.yml` | The CI job that gates, via red CI. Copy it to `.github/workflows/` |
| `/code-review` · `/security-review` · `/design-review` | The same shape, other surfaces |

---

*Part of [ApexYard](https://github.com/me2resh/apexyard) — multi-project SDLC framework for Claude Code · MIT.*
