---
id: AgDR-0123
timestamp: 2026-09-11T11:00:00Z
agent: claude
model: claude-opus-5
trigger: user-prompt
status: accepted
---

# Email templates get a deterministic linter plus an advisory reviewer, and the gate runs in CI rather than as a merge hook

> In the context of adding email-template review to the framework - a surface
> whose content typically lives in a database column or a third-party editor
> rather than in the repo, with no build step and no code review on the way in -
> facing a choice between one agent that reviews templates end to end and a
> split between a script and an agent, I decided to **split the work by
> decidability**: a deterministic Python linter owns every rule with a right
> answer, an advisory agent (Barid) owns everything requiring judgement, and the
> **gate is a CI job rather than a `PreToolUse` merge hook** - accepting that a
> local `gh pr merge` is no longer the enforcement point, in exchange for a gate
> that also catches a human merging in the GitHub UI and that adds no new
> trust-chain surface.

## Context

The operator asked for "an agent, a skill" that builds and audits all mail
designs, living in apexyard so it serves any adopter rather than one project.
Two design questions had to be answered before writing anything.

**What should an agent actually do here?** Email has an unusually large set of
mechanical rules: `rgba()` is unsupported in Outlook, an `<img>` without `alt`
is unreadable when images are blocked, a brand webfont with no fallback renders
as Times in Gmail, a button fill below 3:1 against its own band has no visible
edge. Each is *decidable*. A language model asked to check thirty such rules
across twenty templates will get most of them right - which is the worst
available outcome, because nobody can tell which ones.

**Where does the gate live?** The operator ruled: lint gates, judgement advises.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| One agent reviews templates end to end | Simplest to build; one prompt | Unreliable exactly where reliability is the whole point. No way to tell a missed `rgba()` from an absent one |
| Linter + agent, gate as a `PreToolUse` hook on `gh pr merge` | Matches the existing merge-gate family; blocks in-session merges | Only fires when the merge goes through Claude Code - a human merging in the GitHub UI bypasses it. Needs to re-fetch each changed template from the forge at PR HEAD, because the local tree is usually not the PR branch. Adds a hook and a `settings.json` matcher, both trust-chain surfaces needing a security pass |
| **Linter + agent, gate as a CI job** | The existing `block-merge-on-red-ci.sh` turns a red check into a merge gate for free, covering both merge shapes. Catches UI merges. Author sees the finding on push, not at merge. **No new trust-chain surface** | The lint no longer blocks at the local merge call itself; it blocks one step earlier, via CI |

## Decision

Chosen: **linter + advisory agent, gated in CI.**

- `.claude/hooks/mail-lint.py` - the deterministic pass. Two tiers:
  **universal** rules (true of HTML email regardless of brand) ship ON, so a
  fresh adopter gets value with zero config; **brand** rules (palette, font
  fallbacks, families, CTA grounds) ship EMPTY and **skip loudly** rather than
  guessing a house style. Config at `mail_design` in
  `project-config.defaults.json`, overridable per adopter.
- `.claude/agents/mail-reviewer.md` - Barid. Judgement only, with the lint
  output already in its brief so it never re-derives what a formula settles.
  No Write/Edit tools, no marker.
- `.claude/agents/mail-author.md` - Katib. The authoring half, added after the
  first templates were built (see Postscript 4). Has Write/Edit, runs the linter
  on its own output, and is explicitly barred from ruling on open design
  questions or "fixing" accepted exceptions.
- `.claude/skills/mail-review/SKILL.md` - drives the review pair.
- `golden-paths/pipelines/mail-lint.yml` - the gate.

**The highest-value thing Barid does is not aesthetic.** It asks whether a
clinical message is wearing a marketing template - because that inherits the
marketing **consent gate**, and patients who never opted in then silently stop
receiving mail they medically need. No regex reads intent; no error is raised
when this fails.

## Consequences

- **No approval marker exists on this surface, so there is nothing to forge.**
  The failure mode `pr-workflow.md` devotes a page to - an agent writing its own
  approval - cannot occur, because the gate is a script reading a template
  rather than an agent writing a file. This was a side effect of the CI choice,
  not its motivation, and it is the strongest argument for it.
- A local `gh pr merge` no longer blocks on mail lint directly. It blocks
  because CI is red, one step earlier. Anyone disabling the workflow removes the
  gate - the same exposure every CI-based check carries.
- **"Nothing to forge" is true of markers, but the trust does not vanish - it
  moves to the workflow file and to `project-config.json`.** Whoever can edit
  either can weaken the gate. Security review made this concrete: config chose
  which files were read, and a config-supplied regex chose what was extracted
  from them. Both are now bounded (containment check, compile guard, timeout),
  but the honest statement of the decision is *the forgeable surface moved and
  shrank*, not that it disappeared.
- **The gate used to go green when it was misconfigured**, in three ways: no
  `mail_design` block, a partial override silently dropping `template_globs`
  (the merge is shallow), or zero templates matched. All three now fail closed
  with **exit 2**, kept distinct from exit 1 so "your config is wrong" never
  looks like "your templates are wrong". CI passes `--require-match`, because a
  job that only runs on template changes finding no templates means the globs
  and the workflow's `paths:` filter have drifted apart. That flag only catches
  **total** drift, though, and the likelier shape is partial - adopters
  accumulate globs, so one live glob and two stale ones exited 0 with nothing
  said and whole directories silently unlinted. Each glob matching no files is
  now named individually in SKIPPED.
- The `paths:` filter in the workflow duplicates `mail_design.template_globs`.
  Two places to keep in sync; GitHub Actions cannot read the JSON to build its
  own trigger.
- A green check does not mean "fully audited" - it means nothing that ran
  failed. The workflow writes the skipped-check list into the job summary so a
  reader of a green run can see the difference.
- Adopters with templates in a database or an ESP account get nothing yet; v1
  reads HTML files from the repo. Those are adapters on the same linter.

## Artifacts

- `.claude/hooks/mail-lint.py`, `.claude/hooks/tests/test_mail_lint.sh` (29 cases)
- `.claude/agents/mail-reviewer.md` (Barid), `.claude/agents/mail-author.md` (Katib)
- `.claude/skills/mail-review/SKILL.md`
- `golden-paths/pipelines/mail-lint.yml`
- `.claude/project-config.defaults.json` → `mail_design`

## Postscript - two bugs the build found in itself

Both are in the test suite as regressions, and both argue for the split this
AgDR records.

**The nesting bug.** The first draft attributed a colour to "the enclosing
cell". The canonical email button is a `<td background:orange>` inside a
`<td background:white>`, so the button's white label was measured against the
white band and reported 1:1 - a false positive on a correct template. A linter
the team learns to disbelieve stops being a gate. Fixed with a stack-based
scan that resolves the *nearest* background-carrying ancestor.

**The quote bug, written twice.** The font-fallback check used
`font-family:[^;"']+`. A stack spelled `font-family:"Playfair Display"`
produces **no match at all**, so the loop never ran and the exact violation the
check exists to catch walked straight through. The identical defect had been
independently written into the sibling guard in the project repo and was caught
there in review. Two authorings, same mistake: a character class that excludes
a quote cannot see a quoted font name.

Neither would have been caught by a model reading templates and forming an
opinion - which is the case for the deterministic half. And neither would have
been caught without negative tests, which is the case for writing them.

## Postscript 2 - what review found that self-review did not

The first version passed its own 12 tests and was wrong in ways those tests
could not see. Recording it because the lesson generalises past this file.

**It was blind on the most common authoring style.** `bgcolor="#E3FF6B"` - the
attribute Outlook actually honours, emitted by every email framework - was
invisible, because only the CSS `background-color:` form was read. On a
`bgcolor` template it missed a 1.6:1 body AND a CTA on the wrong band, while
raising a *false* "no signature band" for a band that was right there. Failing
in both directions at once is the worst thing a linter can do.

**Three-digit hex was invisible.** `color:#ddd` on `background-color:#eee` -
off-palette, 1.2:1 - reported clean.

**A documented gate did not exist.** SKILL.md described a
`require-mail-lint.sh` merge hook while this AgDR, in the same PR, argued at
length for CI *instead of* a hook. Both reviewers caught it independently. A
reader believing a gate protects them when it does not is worse than no gate.

**The skip-loudly contract held for four checks out of twenty-three**, while
four documents asserted it universally - and the CI summary went on to print
"All configured checks ran." The step written to prevent silence about unrun
checks was the one producing it.

**Half the checks could be deleted with the suite still green.** A mutation test
over all 23 found 12 survivors, including palette detection and eight of ten
universal rules. The tests that existed were good; there were not enough of
them. There is now one negative case per check.

**And the fix for one of those tests exposed a real defect**: `requires_photo`
only asserted "an `<img>` exists", which the logo in the header strip always
satisfied - so the check could never fail. It now discriminates by width,
because a photograph in this layout is full-bleed and a logo is not.

The pattern across all six: **self-review verified the code did what I meant;
review verified it did what it claimed.** The gap between those two is where
every one of these lived.

## Postscript 3 - the colour grammar, and a gate with a switch on it

A third review round found four (a fifth, below, turned out not to be a
defect at all), and the pattern is sharper than in postscript 2. Every one lived in code written to *fix* an earlier finding.

**The linter could be switched off from inside a template.** Comments were
found with `<!--.*?-->`. A `<!--` inside a quoted attribute value is literal
text to every mail client, but that search reads it as a comment opener and
blanks everything to the next `-->` - out of the contrast, CTA and band checks
alike. A body at 1.12:1, invisible to the reader, reported `clean`. The
stripping had been added for the MSO ghost-table false positive, which is a
real and standard idiom; the mistake was reaching for a regex to answer a
question about document structure. It is now a tag-aware scan, an unterminated
`<!--` is reported rather than obeyed (honouring it would hand the same
primitive back through a shorter door), and the scan is applied **once in
`lint_file`** so every check shares one definition. Before that it was applied
in exactly one place, so the same run held two opinions: `parse_elements`
stripped and the universal checks did not, and genuinely dead markup produced
ERROR-severity false positives. One root cause, wrong in both directions.

**The contrast layer read hex and nothing else.** `bgcolor="white"` - the most
common ground spelling in hand-written email - was invisible, so a 1.03:1 body
reported clean *while the report listed `contrast` among the checks that ran*.
Same for `rgb()`, `hsl()`, and any colour not sitting immediately after the
colon. The second half was worse: the pattern lacked the word boundary its
sibling had been given, so `#12345` normalised to `#112233` - a colour
appearing nowhere in the document - and `#ff0000ff` had its alpha silently
dropped, both then reported as *measured* ratios against real line numbers.
There is now one resolver for hex-3/6, the named table, `rgb()` and `hsl()`;
anything else becomes an `unreadable-colour` finding instead of a silent drop;
and `contrast` is only claimed as run when a comparison actually happened.

**The photograph rule failed CI on correct templates.** `\d+` read `100` out of
`width="100%"` - the spelling every ESP emits for a full-bleed hero. The MSO
fix and the photo fix shipped in the same commit, one removing an
ERROR-severity false positive and the other introducing one.

**The CI summary step was reported broken and was not.** A review found the
two `python3 -c` programs indented to sit inside the YAML block and concluded
they would raise `IndentationError` under `set -e` on every run that produced
a report. The first version of this postscript recorded that as fact. It is
wrong: a YAML block scalar strips the block's *common* indent, and those lines
sat at exactly it, so Python received an unindented program. Extracting the
step with real block-scalar semantics and running it under `bash -e` exits 0
and writes the correct summary. The reproduction that appeared to confirm the
bug used a shell heredoc, which preserves the indentation YAML removes - it
tested the harness, not the step.

The change to heredocs stands, as **defensive** rather than corrective: it is
robust against a future re-indent of the workflow, which the `-c` form is not.
Recorded at length because an AgDR is what someone trusts years later, and a
confidently-worded false entry in one is worse than no entry.

**The comment policy has a cost, and it is recorded here because
`SKILL.md` says it is.** Stripping comments before every check means content a
client *does* render but a linter treats as commented is not checked. Two
cases: an MSO conditional comment (Outlook renders it), and `<style><!-- ...
--></style>`, a legacy email idiom where CSS ignores the `<!--` as a CDO token
and applies the rules between. The second was found as a live suppression -
`@import`, a third-party font host and `display:flex` all went unreported
inside one - and is closed by carrying rawtext state, so `<style>` content is
no longer stripped. The MSO case remains: it is the narrower of the two, and
the alternative (not stripping) reintroduces the ghost-table false positive
this file was built to avoid. Stated plainly so nobody has to infer it from a
diff.

**Mutation coverage was overstated.** The previous round claimed "one negative
case per check"; a sweep neutering each of 45 emission sites found **24
survivors**, including `path-escape` (the security fix with zero tests), the
CTA's measured floor, and the loop registering every universal skip - which is
to say the *fix for* the skip-loudly finding was itself unverified. The suite
is now **60 cases, and a re-run of the same sweep kills all 50 sites** - every
finding and every skip this file can emit is pinned by at least one test that
fails without it.

Three rounds of this metric are worth recording together, because the trend is
the argument for measuring it at all: 12 survivors of 23 sites, then 24 of 45,
now 0 of 50. Test *count* moved steadily upward the whole time (12 → 29 → 60)
and said nothing useful; only the sweep distinguished "more tests" from "more
covered".

The lesson that generalises: **each of these was introduced by a fix.** Review
caught them because review runs the code against inputs the author did not
imagine. The three fixes in postscript 2 were verified the same way and held;
these five were not, and did not.

---

*Part of [ApexYard](https://github.com/me2resh/apexyard) — multi-project SDLC framework for Claude Code · MIT.*

## Postscript 4 - the authoring half, and why it is a separate agent

Barid was built first, and the gap showed the moment real templates were
written: the framework could *judge* an email template and *gate* it, but the
thing that produced it was an unbriefed general agent rediscovering the spec
each time. The linter catches what is mechanically wrong; nothing was carrying
what is easy to get wrong on the way in.

**Katib is deliberately a second agent rather than Write tools bolted onto
Barid.** Three reasons:

1. **A reviewer with Write tools stops being a reviewer.** Barid's value is that
   it cannot quietly fix what it finds, so its findings have to survive being
   read. Giving one agent both roles produces a template that has been graded
   by its own author.
2. **The briefs point in opposite directions.** Barid is told *not* to re-derive
   anything the linter settles, because re-checking mechanics is where a model
   wastes its judgement. Katib must think about exactly those mechanics, because
   it is the one introducing them.
3. **Only the author can create a defect.** The asymmetry is worth encoding:
   Katib's brief is mostly prohibitions and process, Barid's is mostly axes of
   judgement.

The prohibitions are the load-bearing part, and each is a real failure rather
than a hypothetical one: reporting a template clean when the brand checks had
silently skipped; "improving" a contrast pair that a specification records as a
deliberate accepted exception; and answering a question the spec marks `[open]`
by writing a template that implies an answer. All three read as diligence in a
diff, which is what makes them worth naming in a brief.

Katib is also told to leave a guard behind whenever it fixes something a
document was already supposed to prevent, and to watch that guard fail before
trusting it - the same rule this AgDR's own postscripts arrived at the hard way.
