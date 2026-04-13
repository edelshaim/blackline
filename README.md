# Blackline Tool

A local document blacklining CLI aimed at legal workflows.

Version `0.2.0` is the first DOCX-native release.

## What it does

- Compares **Original** and **Revised** files in `.docx` or `.txt`.
- Builds DOCX output by cloning the original `.docx` structure and applying the blackline in place.
- Emits real Word tracked changes in native DOCX output so reviewers can accept or reject revisions in Word.
- Preserves Word-native layout features much more closely:
  - paragraph styles
  - numbering and indentation
  - tables, inserted rows, and moved rows
  - headers and footers
  - text boxes
  - footnotes and endnotes when note parts are present
- Preserves special Word content more safely during revised paragraphs, including bookmarks, fields, and cross-reference markup.
- Detects moved blocks and reports them separately from plain insert/delete noise, including native paragraph moves and table-row reorderings in DOCX output.
- Produces continuous legal-style blackline documents in:
  - `HTML`
  - `DOCX`
  - `PDF`
  - `JSON`
- Uses a shared comparison model so DOCX, HTML, PDF, and JSON are driven by the same structural diff.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
blackline original.docx revised.docx --formats html,docx,pdf,json --output-dir ./output
```

## Options

- `--formats html,docx,pdf,json`
  `all` expands to `html,docx,pdf,json`.
- `--output-dir ./output`
- `--base-name contract_redline`
- `--profile default|legal|contract|litigation|factum|presentation`
- `--strict-legal`
  Alias for `--profile legal`.
- `--ignore-case`
- `--ignore-whitespace`
- `--ignore-smart-punctuation`
- `--ignore-punctuation`
- `--ignore-numbering`
- `--no-detect-moves`

## Profiles

- `default`
  No normalization beyond standard tokenization.
- `legal`
  Ignores case-only edits, normalizes smart quotes/dashes, and prefers substantive alignment.
- `contract`
  Starts with `legal` behavior and also suppresses clause-numbering churn.
- `litigation`
  Starts with `contract` behavior and also suppresses whitespace-only churn.
- `factum`
  Matches `litigation` defaults for appellate-style documents.
- `presentation`
  Suppresses case, punctuation, smart punctuation, and whitespace noise.

## Output structure

HTML, DOCX, and PDF outputs all use the same primary structure:

1. A minimal header with source files and active comparison profile.
2. A continuous blacklined document in document order.
3. Inline redline markers:
   additions in blue double underline
   deletions in red strikethrough

For native `.docx` to `.docx` comparisons, the generated DOCX also carries real Word revision XML (`w:ins` / `w:del`) and native move markup where supported, so Word review features remain available alongside the rendered blackline.

## Notes

- The tool is fully local and has no network dependency.
- When both inputs are `.docx`, generated DOCX output is produced from the original file rather than from a synthetic rebuild.
- Native DOCX output preserves tracked changes across body content, headers, footers, tables, and special field-heavy paragraphs where possible.
- Table-row moves are represented as row-level insert/delete revisions in DOCX output; paragraph moves use Word move markup.
- PDF output will be converted from the generated DOCX when `soffice` or `libreoffice` is available.
- If no Office converter is available, PDF falls back to the internal renderer with the same diff model.
- JSON output is intended for downstream automation and testing.

## Development checks

Run tests from the repository root:

```bash
python -m py_compile src/blackline_tool/cli.py src/blackline_tool/core.py src/blackline_tool/strict.py tests/test_cli.py tests/test_core.py
PYTHONPATH=src pytest -q
```
