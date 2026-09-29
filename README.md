# notebooklm-book-pipeline

Chapter-level NotebookLM study artifact pipeline.

## What it does

Split a book into chapter sources and prepare a manifest for Ukrainian audio overviews, slide decks, quizzes and infographics. Use `python3 scripts/book_pipeline.py --book /path/to/book.epub --mode prepare` first. Full execution requires an authenticated NotebookLM CLI; see `SKILL.md`. EPUB is preferred because chapter structure is more reliable.

## Repository

- `SKILL.md`: usage and behavior

Source files are in `scripts/`, `references/`, `agents/` or `static/` where applicable.

## Notes

Commands that use external services require your own authenticated account. Review generated outputs before sharing them.
