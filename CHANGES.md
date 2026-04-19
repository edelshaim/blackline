# Review Shell v2 — Dense-Pro Redesign

Port of the reference design in `Blackline Standalone.html` into the
string-template server at `src/blackline_tool/web.py`. Ships as the **default**
at `/runs/<id>`; legacy shell remains reachable at `/runs/<id>?v=1` for one
release so muscle-memory links still work.

No backend routes were renamed. No JSON schema fields were removed. No new
Python dependencies were added. Google Fonts is loaded via `<link>`; the old
shell's Google Fonts import remains for its Inter + Source Serif stack.

## What shipped

### Top bar (`.top`)
Five bordered sections separated by 1px vertical rules:

1. **Brand** — small ink square + "Blackline" + mono "Compare" tag.
2. **Run metrics** — mono `t · vis · pend` key/value pairs (pending in mod color).
3. **Export ▾** dropdown replacing the five loose `Download *` buttons, with
   `Final document` (Final .docx · ⌘E) and `Raw compare` (.docx / .html / .json / .pdf)
   groups. Clicks outside close it. Keyboard: `⌘/Ctrl+E` jumps to the
   `/api/runs/<id>/export-clean` endpoint directly. **Zen** sits next to it.
4. **Navigation** — `Nav` label, Prev/Next with `K`/`J` keycaps, mono jump input
   with `G` keycap (Enter to jump).
5. **Right-aligned** — Shortcuts ghost button + `Inline/Split/Tri` segmented
   control (`aria-pressed="true"` is the dark pill).

### Left rail (`.rail`)
- Sticky search with pure-CSS magnifier (circle + rotated 1.5px line pseudo-elements).
  `/` focuses; `Esc` blurs. Below it a mono hint strip: `search / · browse B · jump G`.
- Four collapsible groups (click the 10px all-caps header to toggle
  `.collapsed`; caret rotates -90°):
  - **Scope** — `Formatting-only` toggle chip with `<active>/<total>` count,
    3 keycap buttons (`Next pend N`, `Next fmt M`, `Next changed C`),
    4px progress bar bound to visible/changed ratio.
  - **Type** — `Changes / Moves / Replaced / Inserts / Deletes / All` chips with
    colored swatches and live counts.
  - **Facets** — `Any facet` + every facet the current run actually has, with
    counts (facets with zero sections are omitted).
  - **Decisions** — `Any / Pending / Accepted / Rejected` chips, then
    `Accept vis / Reject vis / Clear vis` and `Undo last / Next undecided`
    row, plus a mono guidance line.
- **Section list** — indexed rows (`01 Preamble` etc.), each with a 4-cell
  mini density bar showing change type per section.

### Document stage (`.stage`)
- `--paper-2` background, 28px top / 60px bottom stage padding.
- White doc card: 860px max-width, 64×84px padding, 1px border, 20px-blur soft shadow.
- Doc title in `--accent`, centered; mono uppercase subtitle underneath (`original.docx → revised.docx`).
- Paragraphs rendered as `<p class="p-interactive modbar kind-{kind}">` with
  `<span class="pnum">¶N</span>`. Inline ins/del/equal spans rendered from
  `section.combined_tokens` when available (new metadata field — see below).
  Modbar paragraphs are clickable and open the inspector; unchanged paragraphs
  close it.

### Slide-in inspector (`.inspector`)
Lives inside `.stage`, absolutely positioned at the right edge.
- Default: `transform: translateX(100%)` hidden state + `.22s cubic-bezier(.2,.7,.2,1)` transition.
- Click a changed paragraph → inspector slides in; clicking an unchanged paragraph or pressing `Esc` dismisses.
- **Header** — color-tinted status pill (`kind-insert` green / `kind-delete` red / `kind-replace` orange), paragraph title, mono section context, `×` close.
- **Body** (each mono `LABEL ─────` divider):
  1. Classification — tags (`format-only` highlighted in `--accent-soft`).
  2. Formatting deltas — `--paper-2` mono delta box (stub: lists
     `format_change_facets` verbatim; see TODO).
  3. Metadata — key/value rows (`location`, `container`, `kind`,
     `original`/`revised` labels).
  4. Compare — original card (2px red left border, red dot) + revised card
     (green), headers in mono uppercase 9.5px.
- **Footer** — Decision label + `Accept (A) / Reject (R) / Clear` grid.
  Buttons reflect current decision via `aria-pressed`; keyboard shortcuts
  `A/R/U` dispatch while the inspector is open.

### Status bar (`footer.status`)
Fixed 26px strip: green dot + `Compare complete` + `original ↔ revised` + live
change count + decided/pending counts (pending in mod color) + spacer +
`Progress` label + minibar (accent fill) + `⌘K for commands`. All mono 10.5px
uppercase; separators are 1×12px `--line` rules.

## What was wired through

- `section.combined_tokens` was **added to the metadata JSON** (`core.py` section
  payload) so the v2 document card renders inline `<span class="ins/del>` word
  tokens without re-opening the DOCX client-side. This is a purely additive
  change — existing consumers ignore the new field. Covered by the new
  `test_run_metadata_includes_combined_tokens` test.
- Top-bar metrics (`t`, `vis`, `pend`) and status bar counts bind to
  `BL.meta.summary` + `BL.decisions`, refreshing after every decision.
- Export dropdown builds download URLs from `BL.meta.files` — formats the run
  didn't produce are disabled.
- Bulk actions (`Accept vis / Reject vis / Clear vis`) call
  `/api/runs/<id>/decisions/batch` with the **currently visible** changed
  section indexes (honors all active filters).

## What was stubbed with TODOs

- **Author email** on each change — not emitted by `core.py` today. Omitted
  from the inspector `Metadata` section. To implement: surface reviewer
  identity from DOCX `w:author` attributes during the diff pass, add a
  `author` field per section. Marked with TODO in the renderer.
- **Revised timestamp per change** — same story as author. Not emitted by
  `core.py`; omitted from the inspector. DOCX provides this per `w:ins`/`w:del`
  via `w:date`.
- **Per-key formatting deltas** (`layout.align: left → justify`, etc.) — the
  metadata currently emits `format_change_facets` as a flat list of facet
  names, not structured delta tuples. The inspector renders each facet as a
  row with `<span class="muted">changed</span>` placeholder. TODO comment in
  `renderInspector`. To implement: emit `formatting_deltas: [{"key": str,
  "before": str, "after": str}, …]` from the diff engine.
- **`t` metric** in the top bar — shows `created_at` as a relative age
  (`2m ago` / `3h ago`), not the compare duration the mock suggests. The
  duration isn't captured anywhere today.

## Intentional behavior changes

- **Filter kind default** is still `changed` (unchanged paragraphs hidden from
  the section list) — matches legacy.
- **Inspector auto-opens on click** for changed paragraphs; legacy required a
  separate "inspect" gesture.
- **Esc** now closes the inspector globally. Legacy bound Esc to dismissing
  the shortcut overlay.
- **Export buttons** are consolidated behind one dropdown. The four loose
  `Download *` anchors from the legacy top bar are gone from v2 (still present
  in `?v=1`).

## Migration notes

- `/runs/<id>` now returns v2. Bookmarked links continue to work.
- `/runs/<id>?v=1` returns the legacy shell. Scheduled for removal once v2
  has burned in across the workflows that matter.
- `metadata.json` files on disk from before this change do **not** include
  `combined_tokens`. The v2 document renderer falls back to `revised_text`
  (displays the post-change paragraph with no inline spans, modbar still
  visible). Regenerate a run to see inline ins/del highlighting.
- No changes to `decisions.json` format. No changes to the compare API or the
  download routes.

## Tests

- `tests/test_web.py` — 7 tests (3 new):
  - `test_review_shell_v2_has_new_chrome` — asserts the full v2 top bar, rail,
    stage, and inspector markup.
  - `test_v2_and_legacy_shells_diverge` — confirms the two shells emit
    different markup and tokens.
  - `test_run_metadata_includes_combined_tokens` — guards the new metadata field.
- All 47 tests pass. `ruff check` clean.
