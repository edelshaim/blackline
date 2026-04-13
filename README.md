# Blackline Tool

A local document blacklining CLI aimed at legal workflows.

Version `0.2.0` is the first DOCX-native release.

## What it does

- Compares **Original** and **Revised** files in `.docx` or `.txt`.
- Builds DOCX output by cloning the original `.docx` structure and applying the blackline in place.
- Preserves Word-native layout features much more closely:
  - paragraph styles
  - numbering and indentation
  - tables and inserted table rows
  - headers and footers
  - text boxes
  - footnotes and endnotes when note parts are present
- Detects moved blocks and reports them separately from plain insert/delete noise.
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

## Notes

- The tool is fully local and has no network dependency.
- When both inputs are `.docx`, generated DOCX output is produced from the original file rather than from a synthetic rebuild.
- PDF output will be converted from the generated DOCX when `soffice` or `libreoffice` is available.
- If no Office converter is available, PDF falls back to the internal renderer with the same diff model.
- JSON output is intended for downstream automation and testing.

## Development checks

Run tests from the repository root:

```bash
python -m py_compile src/blackline_tool/cli.py src/blackline_tool/core.py src/blackline_tool/strict.py tests/test_cli.py tests/test_core.py
PYTHONPATH=src pytest -q
```
