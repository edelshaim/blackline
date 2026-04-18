# UX Improvement Loop Runbook ("Ralph Wiggum Loop")

## Purpose
Continuously improve UX in small, testable iterations.
Each loop should ship one high-impact UX improvement with verification.

## Loop Definition
For each run:
1. Identify one UX pain quickly (heuristic review + quick smoke path).
2. Implement one focused UX improvement.
3. Verify quality gates:
   - `make lint`
   - `make test`
   - quick web review smoke path
4. Record what improved and what still feels awkward.

## Execution Model
- Run via subagents at least **3 iterations**.
- Execute **sequentially** to avoid merge conflicts.
- Main agent integrates each run and performs final verification.

## Subagent Ownership Rules
- Primary write scope:
  - `src/blackline_tool/web.py`
  - `tests/test_web.py`
- If another file must change, explain why in the subagent report.
- Subagents are not alone in the codebase:
  - do not revert unrelated edits
  - adapt to existing changes

## Planned Iterations

### Run 1 — Visual Clarity
Focus:
- improve visual hierarchy
- improve spacing/scanability
- make state cues clearer (selected, decided, filtered)

Acceptance:
- no regressions in review flow
- cleaner readability in nav + inspector

### Run 2 — Navigation Speed
Focus:
- reduce click/keypress friction
- improve jump/filter keyboard flow
- improve movement between relevant changes

Acceptance:
- faster section traversal
- more predictable keyboard behavior

### Run 3 — Decision Workflow Polish
Focus:
- reduce friction in accept/reject/pending actions
- improve progress and completion clarity
- tighten inspector + list synchronization around decisions

Acceptance:
- decisions feel faster and clearer
- completion state is obvious

## Quality Gates (per run)
1. `make lint`
2. `make test`
3. quick smoke check of review shell interactions touched in run

## Reporting Template (per run)
- Pain identified
- Change implemented
- Why it improves UX
- Files touched
- Verification results
- Follow-up opportunity for next run

## Integration Sequence
1. Run subagent iteration.
2. Review and integrate edits.
3. Run quality gates.
4. Log summary.
5. Move to next run.
