# Blackline Tool (MVP)

A simple, local document blacklining CLI aimed at legal workflows.

## What it does

- Compares **Original** and **Revised** files in `.docx` or `.txt`.
- Produces redline outputs in:
  - `HTML` (clean legal-style inline redline: insertions blue double underline; deletions red strikethrough)
  - `DOCX` (same visual markers)
  - `PDF` (same visual markers, best-effort within PDF renderer constraints)
- For `.docx`→`.docx`, preserves baseline run-level formatting (e.g., bold/italic/font settings) and only marks substantive token changes.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

If CLI options appear missing (for example `--strict-legal` is unrecognized), reinstall from the repo root:

```bash
pip install -e .
```

## Usage

```bash
blackline original.docx revised.docx --formats all --output-dir ./output
```

Options:

- `--formats html,docx,pdf` (or `all`)
- `--output-dir ./output`
- `--base-name contract_redline`
- `--strict-legal` (aliases: `--strict_legal`, `--strict-legal-mode`; suppresses non-substantive edits like case-only or typographic quote/dash normalization)
- `--strict-legal` (suppresses non-substantive edits like case-only or typographic quote/dash normalization)

Example:

```bash
blackline old_contract.docx new_contract.docx --formats html,docx --base-name msa_redline
```

## Notes

- This is an MVP with improved block alignment: it compares paragraph order, aligns changed blocks, then performs word-level diff.
- It is fully local and has no network dependency.
- Advanced handling for tables/footnotes/styles can be added in future phases.


## Development checks

Run tests from the repository root:

```bash
PYTHONPATH=src pytest -q
```
