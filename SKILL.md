---
name: notebooklm_book_pipeline
description: Process an attached book into chapter-level NotebookLM notebooks and artifacts. Use when the user attaches a book and wants each main chapter turned into a long Ukrainian audio overview, a detailed slide deck, three quizzes for chapter thirds, and one infographic with clear NotebookLM artifact names.
---

# NotebookLM Book Pipeline

Use this skill when the user attaches a book and asks to turn it into structured
NotebookLM learning artifacts.

## Default Behavior

1. Treat the attached book as the input file.
2. Extract text from the book.
3. Split the book into main chapters.
4. Skip front matter and back matter by default:
   - table of contents
   - preface/foreword
   - introduction/вступ
   - acknowledgements
   - appendices
   - bibliography/references
   - index/glossary
5. Renumber retained main chapters sequentially.
6. For every retained chapter, create a dedicated NotebookLM notebook.
7. In that chapter notebook, add:
   - one full chapter source
   - three part sources split by paragraph boundaries
8. Generate and rename these artifacts:
   - `Розділ NN - Аудіоподкаст - <chapter title>`
   - `Розділ NN - Презентація - <chapter title>`
   - `Розділ NN - Тест 1 - Частина 1 - <chapter title>`
   - `Розділ NN - Тест 2 - Частина 2 - <chapter title>`
   - `Розділ NN - Тест 3 - Частина 3 - <chapter title>`
   - `Розділ NN - Інфографіка - <chapter title>`

## Commands

Prepare only, without creating NotebookLM notebooks:

```bash
python3 scripts/book_pipeline.py \
  --book "/path/to/book.epub" \
  --title "Book title" \
  --mode prepare
```

Execute the full NotebookLM workflow:

```bash
python3 scripts/book_pipeline.py \
  --book "/path/to/book.epub" \
  --title "Book title" \
  --mode execute \
  --notebooklm-bin "notebooklm"
```

Limit execution while testing:

```bash
python3 scripts/book_pipeline.py \
  --book "/path/to/book.epub" \
  --mode execute \
  --max-chapters 1
```

## Output Structure

The script writes a run directory under:

```text
outputs/book_pipeline/<book_slug>/<timestamp>/
```

Expected files:

- `book_pipeline_manifest.json`
- `notebooklm_plan.md`
- `chapters/chapter-XX-<slug>.md`
- `chapters/chapter-XX-<slug>/part-01.md`
- `chapters/chapter-XX-<slug>/part-02.md`
- `chapters/chapter-XX-<slug>/part-03.md`
- `artifacts/chapter-XX-<slug>/...` when `--mode execute` is used

## Operational Notes

- Use `--mode prepare` first for a new book if chapter detection quality is
  uncertain.
- Prefer EPUB when available. EPUB keeps table-of-contents and reading-order
  structure, which makes chapter detection more reliable than PDF extraction.
- DOCX, PDF, Markdown, plain text, and RTF are supported fallbacks. DOCX works
  best when chapter titles are real text headings or clear numbered lines.
- Use `--keep-intro` only when the introduction is a real content chapter.
- If PDF extraction fails, install `pypdf` into the Python environment used to
  run the script.
- The script uses explicit NotebookLM notebook IDs and source IDs, not shared
  `notebooklm use` context.
- Long artifact generation can hit Google quota/rate limits. The manifest keeps
  per-artifact errors and continues with later artifacts where possible.
