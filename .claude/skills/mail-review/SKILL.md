---
name: mail-review
description: Audit HTML email templates — a deterministic lint pass (contrast, palette, font fallbacks, layout, family rules) plus Barid's judgement pass on register, copy, images-off and consent. The email analog of /code-review.
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
| **1. Lint** — `.claude/hooks/_lib-mail-lint.py` | Contrast (computed), palette, font fallbacks, layout, images, family rules, forbidden copy patterns, placeholders | Mechanically. Same answer every time |
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
python3 .claude/hooks/_lib-mail-lint.py [paths...] [--family <name>] --json
```

Exit 0 clean, 1 findings at `error` severity. Read the `skipped` array in the
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

**The lint is a merge gate; the judgement is not.**
`require-mail-lint.sh` blocks a merge when the PR touches a configured template
path and the linter reports errors. Barid's verdict never blocks.

That asymmetry is deliberate. Lint findings are decidable, so blocking on them
is fair. Blocking a merge on a model's opinion about whether a headline sounds
like the brand generates override pressure, and a gate that is routinely
overridden stops working for the cases that matter.

A useful consequence: the gate is a script reading a template, not an agent
writing a marker — so **there is no approval marker on this surface for anything
to forge**. The failure mode `pr-workflow.md` devotes a page to cannot occur
here.

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
| `.claude/hooks/_lib-mail-lint.py` | The deterministic pass |
| `.claude/hooks/require-mail-lint.sh` | The merge gate |
| `/code-review` · `/security-review` · `/design-review` | The same shape, other surfaces |

---

*Part of [ApexYard](https://github.com/me2resh/apexyard) — multi-project SDLC framework for Claude Code · MIT.*
