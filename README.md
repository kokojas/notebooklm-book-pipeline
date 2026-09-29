# notebooklm-book-pipeline

Prepare chapter sources and a resumable NotebookLM artifact plan.

## What it does

- Extracts EPUB, DOCX, PDF, Markdown, text or RTF
- Keeps main chapters and divides each into three paragraph-aligned parts
- Writes a manifest, chapter files and artifact plan
- Can execute notebook and artifact creation through a NotebookLM CLI

## Quick start

Run `python3 scripts/book_pipeline.py --book /path/to/book.epub --mode prepare`. Inspect the chapter manifest before using `--mode execute --notebooklm-bin /path/to/notebooklm`.

## Browse the repository

- `SKILL.md` — workflow specification
- `scripts` — implementation scripts

## Compatibility

Python 3. EPUB structure is preferred; PDF extraction may require `pypdf`. Execute mode needs an authenticated NotebookLM CLI.

## Quality checks

Python syntax checked. Chapter detection and NotebookLM output should be inspected for each new book.

## Security and privacy

Inspect source files and your own input data before running or publishing outputs. Do not commit credentials, browser cookies, client documents or generated work directories.

## Limits

PDFs with weak heading structure may need manual chapter corrections. Generation quotas may interrupt long runs; the manifest records per-artifact status.
