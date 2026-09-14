---
name: mail-write
description: Write or rebuild an HTML email template to the project's own mail specification — Katib authors it, the deterministic linter proves it, and the report says what was checked and what was not. The authoring counterpart to /mail-review.
---

# /mail-write — Write an email template

`/mail-review` audits templates that already exist. This one produces them.

Email is the surface with no safety net: no build step, no type checker, no
code review on the way in, and content that usually lives in a database column
or a third-party editor rather than in the repo. Once a message is sent it
cannot be corrected. This skill exists so that authoring a template is a
process rather than a recollection.

## Usage

```
/mail-write                                    # asks what to build
/mail-write prescription_ready                 # one template, by key or name
/mail-write --family transactional             # state the family instead of inferring it
/mail-write --rebuild 'emails/**/*.html'       # rebuild an existing set to the current spec
/mail-write --from docs/.../example.html       # start from a judged reference
```

## What it does

| Step | |
|---|---|
| **1. Read the rules** | The project's mail spec (`mail_design.spec_path`), the authority chain above it if there is one, the brand/voice document, and any template the owner has already judged good |
| **2. Settle the family** | Transactional or marketing, decided by whether the message needs consent. If the project has not ruled, it stops and asks |
| **3. Write** | Band structure, copy, tokens, images, plain-text body |
| **4. Prove it** | Runs `mail-lint.py` on its own output and fixes real findings |
| **5. Report** | What it wrote, what the linter checked, **what the linter skipped**, and what it refused to decide |

Then hand the result to `/mail-review` for the judgement pass. Katib does not
review its own work — a template graded by its author is not reviewed.

## The family question, which is the whole point

A message someone receives because they are mid-treatment is **transactional**,
however good its news. A marketing template typically carries a **consent gate**
and an unsubscribe link, so putting a clinical message in one means people who
never opted into marketing stop receiving mail they medically need.

No error. No bounce. No alert. Just silence.

So the family is decided by **whether the message needs consent**, never by how
warm it sounds. If that has not been ruled for the template in hand, this skill
asks rather than guesses.

## What it will not do

Four refusals, each a real failure rather than a hypothetical one, and each
looks like diligence in a diff — which is exactly why they are written down:

- **It will not report a template done without linting it,** and it always
  prints what the linter *skipped*. A linter whose brand checks silently
  skipped produces something almost identical to a pass.
- **It will not "fix" an accepted exception.** Specs record deliberate,
  ruled exceptions — most often a contrast pair someone weighed and accepted.
  Improving one locally overrides a decision and reads as a bug fix to every
  reviewer. The fix for a linter flagging one is the linter's config.
- **It will not answer an open question** by writing a template that implies an
  answer. It escalates, and builds everything that does not depend on it.
- **It will not change how mail is sent.** Designing a message and choosing its
  transport are different decisions.

## Rebuilding a set

`--rebuild` follows one rule above all others: **build exactly one, completely,
and show it to the owner before the other nineteen.** Twenty templates rebuilt
to a misread rule is twenty things to redo; one is a conversation.

It also treats a template key as a published interface. Systems commonly derive
a trigger or metric name from the key, so renaming one can silently stop a flow
firing — it will flag a rename rather than perform one.

## Traps it carries so you do not have to

- **Tokens fail silently.** Substitution is usually a plain regex, so an unknown
  `{{name}}` renders as an empty string and "Hello ," ships. Copy is written to
  survive an empty value, and every token used is declared.
- **Size has a cliff.** Inlining images pushes a message toward the size at
  which a major client clips it, and a clipped mail hides its own footer — where
  the legal and unsubscribe content lives. That is a compliance failure, not a
  cosmetic one.
- **Image formats are not browser formats.** One widely-used client renders
  through an engine with no SVG support at all. The project's ruled format wins
  over what looks fine in a browser.
- **Type is judged in the fallback.** If a template only looks right once the
  web font loads, it is not designed. Line wraps are counted in the fallback
  face too, which is often the wider one.
- **Images off is the default assumption.** Headlines are live text, never baked
  into artwork.
- **Plain text is not the HTML with tags stripped.** It is what text-only
  clients, watches and accessibility tooling render, and it carries full URLs.

## Guards, not notes

When this skill fixes a rule violation that a document was already supposed to
prevent, the document was not enough — so it leaves a test behind. These
templates are edited outside the repo, where prose cannot reach.

Two rules for any guard it writes, both learned the hard way:

- **Watch it fail first.** A green check nobody has seen fail is not evidence.
- **Prove it is looking at something.** A guard that walks a list it builds
  itself reports green when the list is empty, after a rename or a moved file.

## Related

| | |
|---|---|
| `/mail-review` | Audit templates that already exist — the linter plus Barid |
| `.claude/agents/mail-author.md` | Katib, the agent this skill drives |
| `.claude/hooks/mail-lint.py` | The deterministic pass, and the merge gate |
| AgDR-0123 | Why authoring and review are separate agents |

---

*Part of [ApexYard](https://github.com/me2resh/apexyard) — multi-project SDLC framework for Claude Code · MIT.*
