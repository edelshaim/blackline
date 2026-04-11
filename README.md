# Blackline Tool (MVP)

A simple, local document blacklining CLI aimed at legal workflows.

## What it does

- Compares **Original** and **Revised** files in `.docx` or `.txt`.
- Produces redline outputs in:
  - `HTML` (insertions green, deletions red strikethrough)
  - `DOCX` (same visual markers)
  - `PDF` (same visual markers)

## Install

```bash
python -m venv .venv
source .venv/bin/activate
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

Example:

```bash
blackline old_contract.docx new_contract.docx --formats html,docx --base-name msa_redline
```

## Notes

- This is an MVP that compares paragraph order and then does word-level diff inside changed paragraphs.
- It is fully local and has no network dependency.
- Advanced handling for tables/footnotes/styles can be added in future phases.
