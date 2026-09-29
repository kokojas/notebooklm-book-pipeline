#!/usr/bin/env python3
"""Prepare and execute a chapter-level NotebookLM book artifact pipeline."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import posixpath
import textwrap
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree


FRONT_BACK_MATTER_TERMS = (
    "acknowledgement",
    "acknowledgment",
    "appendix",
    "bibliography",
    "contents",
    "copyright",
    "foreword",
    "glossary",
    "index",
    "introduction",
    "notes",
    "preface",
    "references",
    "table of contents",
    "about the author",
    "вступ",
    "введение",
    "додат",
    "зміст",
    "оглавление",
    "передмова",
    "предисловие",
    "подяки",
    "примітки",
    "список літератури",
    "бібліограф",
    "покажчик",
)

BACK_MATTER_TERMS = (
    "appendix",
    "bibliography",
    "glossary",
    "index",
    "notes",
    "references",
    "додат",
    "примітки",
    "список літератури",
    "бібліограф",
    "покажчик",
)

CHAPTER_PATTERNS = (
    re.compile(
        r"^\s*(?:розділ|глава|chapter)\s+([0-9]+|[ivxlcdm]+|[IVXLCDM]+)"
        r"(?:\s+summary)?"
        r"(?:\s*[.:)\-–—]\s*(.{0,140}))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:частина|part)\s+([0-9]+|[ivxlcdm]+|[IVXLCDM]+)"
        r"(?:\s*[.:)\-–—]\s*(.{0,140}))?\s*$",
        re.IGNORECASE,
    ),
)


@dataclass
class Chapter:
    output_index: int
    original_heading: str
    title: str
    slug: str
    text: str
    start_line: int
    end_line: int
    chapter_file: str = ""
    part_files: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Chapter {self.output_index:02d}"


@dataclass
class SkippedSection:
    heading: str
    reason: str
    start_line: int
    end_line: int


@dataclass
class ArtifactRun:
    kind: str
    title: str
    source_files: list[str]
    command: list[str]
    status: str = "planned"
    artifact_id: str | None = None
    output_path: str | None = None
    error: str | None = None


def slugify(value: str, fallback: str = "book") -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9а-яіїєґё]+", "-", value, flags=re.IGNORECASE)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:80] or fallback


def safe_title(value: str, max_len: int = 90) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_len].rstrip(" .,:;-/") or "Без назви"


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace")


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml_data = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_data)
    paragraphs: list[str] = []
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for paragraph in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n\n".join(paragraphs)


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n\n", value)
    value = re.sub(r"(?i)</h[1-6]\s*>", "\n\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value)


def epub_rootfile(archive: zipfile.ZipFile) -> str | None:
    try:
        container = archive.read("META-INF/container.xml")
    except KeyError:
        return None
    root = ElementTree.fromstring(container)
    for element in root.iter():
        if element.tag.endswith("rootfile"):
            path = element.attrib.get("full-path")
            if path:
                return path
    return None


def epub_spine_items(archive: zipfile.ZipFile, opf_path: str) -> list[str]:
    root = ElementTree.fromstring(archive.read(opf_path))
    opf_dir = posixpath.dirname(opf_path)
    manifest: dict[str, str] = {}
    for element in root.iter():
        if element.tag.endswith("item"):
            item_id = element.attrib.get("id")
            href = element.attrib.get("href")
            media_type = element.attrib.get("media-type", "")
            if item_id and href and ("html" in media_type or href.lower().endswith((".xhtml", ".html", ".htm"))):
                manifest[item_id] = posixpath.normpath(posixpath.join(opf_dir, href))

    spine: list[str] = []
    for element in root.iter():
        if element.tag.endswith("itemref"):
            item = manifest.get(element.attrib.get("idref", ""))
            if item and item in archive.namelist():
                spine.append(item)
    return spine


def epub_toc_labels(archive: zipfile.ZipFile, opf_path: str) -> dict[str, list[str]]:
    opf_dir = posixpath.dirname(opf_path)
    ncx_names = [name for name in archive.namelist() if name.lower().endswith(".ncx")]
    if not ncx_names:
        return {}
    labels_by_file: dict[str, list[str]] = {}
    root = ElementTree.fromstring(archive.read(ncx_names[0]))

    def text_content(element: ElementTree.Element | None) -> str:
        if element is None:
            return ""
        return re.sub(r"\s+", " ", "".join(element.itertext())).strip()

    for nav_point in root.iter():
        if not nav_point.tag.endswith("navPoint"):
            continue
        label_element = next((child for child in nav_point.iter() if child.tag.endswith("text")), None)
        content_element = next((child for child in nav_point.iter() if child.tag.endswith("content")), None)
        label = text_content(label_element)
        src = content_element.attrib.get("src", "") if content_element is not None else ""
        if not label or not src:
            continue
        label_lowered = label.casefold()
        is_major_label = (
            bool(re.match(r"^\s*[0-9]{1,2}\s+\S", label))
            or bool(re.match(r"^\s*(?:часть|частина|part)\s+", label_lowered))
            or any(label_lowered.startswith(term) for term in FRONT_BACK_MATTER_TERMS)
        )
        if not is_major_label:
            continue
        href = src.split("#", 1)[0]
        full_href = posixpath.normpath(posixpath.join(opf_dir, href))
        labels_by_file.setdefault(full_href, [])
        if label not in labels_by_file[full_href]:
            labels_by_file[full_href].append(label)
    return labels_by_file


def extract_epub(path: Path) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        rootfile = epub_rootfile(archive)
        if rootfile:
            names = epub_spine_items(archive, rootfile)
            labels_by_file = epub_toc_labels(archive, rootfile)
        else:
            names = []
            labels_by_file = {}
        if not names:
            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
                and "cover" not in name.lower()
            ]
            names = sorted(names)
        for name in names:
            raw = archive.read(name).decode("utf-8", errors="replace")
            labels = labels_by_file.get(name, [])
            text = strip_html(raw)
            if text.strip():
                heading_text = "\n\n".join(f"## {label}" for label in labels if label)
                chunks.append("\n\n".join(part for part in (heading_text, text.strip()) if part))
    return "\n\n".join(chunks)


def extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PDF input requires pypdf. Install it in this environment: "
            "python -m pip install pypdf"
        ) from exc

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"\n\n--- Page {page_number} ---\n\n{page_text.strip()}")
    return "\n".join(pages)


def extract_book_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return read_text_file(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".epub":
        return extract_epub(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".rtf":
        text = read_text_file(path)
        text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
        text = re.sub(r"[{}]", " ", text)
        return text
    raise ValueError(f"Unsupported book format: {suffix}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n\s*--- Page \d+ ---\s*\n", "\n\n", text)
    lines: list[str] = []
    for line in text.split("\n"):
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if re.fullmatch(r"\d{1,4}", cleaned):
            continue
        lines.append(cleaned)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_excluded_heading(title: str, keep_intro: bool) -> bool:
    lowered = title.casefold()
    terms = FRONT_BACK_MATTER_TERMS if not keep_intro else tuple(
        term for term in FRONT_BACK_MATTER_TERMS if term not in {"introduction", "вступ", "введение"}
    )
    return any(term in lowered for term in terms)


def is_back_matter_heading(title: str) -> bool:
    lowered = title.casefold()
    return any(term in lowered for term in BACK_MATTER_TERMS)


def chapter_number(heading: str) -> int | None:
    compact = re.sub(r"^\s{0,3}#{1,6}\s+", "", heading).strip()
    match = re.match(r"^\s*(?:розділ|глава|chapter)\s+([0-9]{1,3})\b", compact, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.match(r"^\s*([0-9]{1,3})[.)]?\s+\S", compact)
    if match:
        return int(match.group(1))
    return None


def detect_heading(line: str, next_line: str | None = None) -> str | None:
    compact = re.sub(r"\s+", " ", line).strip()
    is_markdown_heading = bool(re.match(r"^\s{0,3}#{1,6}\s+", compact))
    compact = re.sub(r"^\s{0,3}#{1,6}\s+", "", compact)
    if not compact or len(compact) > 160:
        return None
    if "____" in compact:
        return None
    if compact.casefold().startswith("introduction to "):
        return None
    if is_markdown_heading:
        markdown_numbered = re.match(r"^\s*([0-9]{1,2})[.)]?\s+(.{3,140})\s*$", compact)
        if markdown_numbered:
            return safe_title(compact)
    for pattern in CHAPTER_PATTERNS:
        match = pattern.match(compact)
        if not match:
            continue
        suffix = ""
        if len(match.groups()) >= 2:
            suffix = (match.group(2) or "").strip()
        suffix_needs_continuation = bool(
            suffix
            and next_line
            and (
                suffix.casefold().endswith((" and", " part", " idea"))
                or len(suffix.split()) <= 3
            )
        )
        if (not suffix or suffix_needs_continuation) and next_line:
            candidate = re.sub(r"\s+", " ", next_line).strip()
            if 3 <= len(candidate) <= 140 and not candidate.endswith("."):
                suffix = f"{suffix} {candidate}".strip()
        return safe_title(f"{compact} {suffix}" if suffix and suffix not in compact else compact)

    lowered = compact.casefold()
    if len(compact) <= 120 and (
        any(lowered.startswith(term) for term in FRONT_BACK_MATTER_TERMS)
        or (is_markdown_heading and any(term in lowered for term in FRONT_BACK_MATTER_TERMS))
    ):
        return safe_title(compact)

    upper_ratio = sum(1 for char in compact if char.isupper()) / max(1, sum(1 for char in compact if char.isalpha()))
    if 4 <= len(compact) <= 90 and upper_ratio > 0.72 and not compact.endswith("."):
        if any(word in compact.casefold() for word in ("розділ", "chapter", "глава")):
            return safe_title(compact.title())
    return None


def split_by_headings(
    text: str,
    keep_intro: bool,
    min_section_chars: int,
) -> tuple[list[Chapter], list[SkippedSection]]:
    lines = text.splitlines()
    heading_positions: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        next_line = next(
            (
                candidate
                for candidate in lines[index + 1 : index + 6]
                if candidate.strip()
            ),
            None,
        )
        heading = detect_heading(line, next_line)
        if heading:
            heading_positions.append((index, heading))

    if len(heading_positions) < 2:
        return [], []

    chapters: list[Chapter] = []
    skipped: list[SkippedSection] = []
    stopped_at_back_matter = False
    first_chapter_idx = 0
    for index, (start, heading) in enumerate(heading_positions):
        end = heading_positions[index + 1][0] if index + 1 < len(heading_positions) else len(lines)
        section_text = "\n".join(lines[start:end]).strip()
        if chapter_number(heading) == 1 and len(section_text) >= min_section_chars:
            first_chapter_idx = index
            break

    for idx, (start, heading) in enumerate(heading_positions):
        end = heading_positions[idx + 1][0] if idx + 1 < len(heading_positions) else len(lines)
        section_text = "\n".join(lines[start:end]).strip()
        if not section_text:
            continue
        if not keep_intro and idx < first_chapter_idx:
            skipped.append(SkippedSection(heading, "before_first_chapter", start + 1, end))
            continue
        if is_back_matter_heading(heading) and chapters:
            skipped.append(SkippedSection(heading, "back_matter_stop", start + 1, end))
            stopped_at_back_matter = True
            break
        if is_excluded_heading(heading, keep_intro):
            skipped.append(SkippedSection(heading, "front_or_back_matter", start + 1, end))
            continue
        if len(section_text) < min_section_chars:
            skipped.append(SkippedSection(heading, "too_short_heading_section", start + 1, end))
            continue
        if stopped_at_back_matter:
            skipped.append(SkippedSection(heading, "after_back_matter", start + 1, end))
            continue
        chapter_index = len(chapters) + 1
        title = cleanup_chapter_title(heading, chapter_index)
        chapters.append(
            Chapter(
                output_index=chapter_index,
                original_heading=heading,
                title=title,
                slug=slugify(f"chapter-{chapter_index:02d}-{title}", f"chapter-{chapter_index:02d}"),
                text=section_text,
                start_line=start + 1,
                end_line=end,
            )
        )
    return chapters, skipped


def cleanup_chapter_title(heading: str, index: int) -> str:
    cleaned = re.sub(
        r"^\s*(?:розділ|глава|chapter|частина|part)\s+([0-9]+|[ivxlcdm]+)"
        r"(?:\s+summary)?"
        r"\s*[.:)\-–—]?\s*",
        "",
        heading,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*[0-9]{1,2}[.)]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*[0-9]{1,2}\s+", "", cleaned)
    cleaned = safe_title(cleaned)
    if cleaned.casefold() == heading.casefold() or len(cleaned) < 3:
        return f"Основний розділ {index}"
    return cleaned


def chunk_without_headings(text: str, target_chars: int, min_chars: int) -> list[Chapter]:
    paragraphs = [para.strip() for para in re.split(r"\n\s*\n", text) if para.strip()]
    chapters: list[Chapter] = []
    current: list[str] = []
    current_chars = 0

    for paragraph in paragraphs:
        if current and current_chars + len(paragraph) > target_chars and current_chars >= min_chars:
            index = len(chapters) + 1
            title = f"Основний розділ {index}"
            chapters.append(
                Chapter(
                    output_index=index,
                    original_heading=title,
                    title=title,
                    slug=slugify(f"chapter-{index:02d}-{title}", f"chapter-{index:02d}"),
                    text="\n\n".join(current),
                    start_line=0,
                    end_line=0,
                )
            )
            current = []
            current_chars = 0
        current.append(paragraph)
        current_chars += len(paragraph)

    if current:
        index = len(chapters) + 1
        title = f"Основний розділ {index}"
        chapters.append(
            Chapter(
                output_index=index,
                original_heading=title,
                title=title,
                slug=slugify(f"chapter-{index:02d}-{title}", f"chapter-{index:02d}"),
                text="\n\n".join(current),
                start_line=0,
                end_line=0,
            )
        )
    return chapters


def split_chapters(
    text: str,
    keep_intro: bool,
    min_chapter_chars: int,
    target_chapter_chars: int,
) -> tuple[list[Chapter], list[SkippedSection], str]:
    chapters, skipped = split_by_headings(
        text,
        keep_intro=keep_intro,
        min_section_chars=min_chapter_chars,
    )
    if chapters:
        for output_index, chapter in enumerate(chapters, start=1):
            chapter.output_index = output_index
        return chapters, skipped, "headings"

    fallback_chapters = chunk_without_headings(
        text,
        target_chars=target_chapter_chars,
        min_chars=min_chapter_chars,
    )
    return fallback_chapters, skipped, "fixed_size_fallback"


def split_into_parts(text: str, part_count: int) -> list[str]:
    paragraphs = [para.strip() for para in re.split(r"\n\s*\n", text) if para.strip()]
    if not paragraphs:
        return [text]
    total = sum(len(para) for para in paragraphs)
    target = max(1, total // part_count)
    parts: list[list[str]] = [[] for _ in range(part_count)]
    part_index = 0
    current_chars = 0
    for paragraph in paragraphs:
        if part_index < part_count - 1 and current_chars >= target and parts[part_index]:
            part_index += 1
            current_chars = 0
        parts[part_index].append(paragraph)
        current_chars += len(paragraph)
    nonempty_parts = ["\n\n".join(part).strip() for part in parts if part]
    if len(nonempty_parts) == part_count:
        return nonempty_parts

    words = text.split()
    if len(words) < part_count:
        return nonempty_parts
    chunk_size = max(1, (len(words) + part_count - 1) // part_count)
    exact_parts = [
        " ".join(words[index : index + chunk_size]).strip()
        for index in range(0, len(words), chunk_size)
    ]
    return [part for part in exact_parts[:part_count] if part]


def write_chapter_files(run_dir: Path, chapters: list[Chapter], part_count: int) -> None:
    chapters_dir = run_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    for chapter in chapters:
        chapter_dir = chapters_dir / f"chapter-{chapter.output_index:02d}-{chapter.slug}"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        chapter_path = chapters_dir / f"chapter-{chapter.output_index:02d}-{chapter.slug}.md"
        chapter.chapter_file = str(chapter_path)
        chapter_path.write_text(
            f"# {chapter.label}: {chapter.title}\n\n{chapter.text.strip()}\n",
            encoding="utf-8",
        )
        chapter.part_files = []
        for index, part_text in enumerate(split_into_parts(chapter.text, part_count), start=1):
            part_path = chapter_dir / f"part-{index:02d}.md"
            part_path.write_text(
                f"# {chapter.label}: {chapter.title} - Part {index}\n\n{part_text}\n",
                encoding="utf-8",
            )
            chapter.part_files.append(str(part_path))


def resolve_notebooklm_bin(explicit: str | None) -> str:
    candidates: list[Path] = []
    if explicit:
        return explicit
    env_bin = os.environ.get("NOTEBOOKLM_BIN")
    if env_bin:
        return env_bin
    which = shutil.which("notebooklm")
    if which:
        return which
    cwd = Path.cwd()
    candidates.extend(
        [
            cwd / ".notebooklm-venv" / "bin" / "notebooklm",
            cwd.parent / ".notebooklm-venv" / "bin" / "notebooklm",
            Path.home() / ".local" / "bin" / "notebooklm",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "notebooklm"


def command_env(notebooklm_bin: str) -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = str(Path(notebooklm_bin).resolve().parent)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_command(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            output, error = process.communicate(timeout=10)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
            output, error = process.communicate()
        combined_timeout = "\n".join(part for part in ((output or "").strip(), (error or "").strip()) if part)
        raise RuntimeError(
            f"Command timed out after {timeout} seconds: {' '.join(command[:4])}"
            + (f"\n{combined_timeout}" if combined_timeout else "")
        ) from exc
    returncode = process.returncode
    output = (output or "").strip()
    error = (error or "").strip()
    combined = "\n".join(part for part in (output, error) if part)
    if returncode != 0:
        raise RuntimeError(combined or f"Command failed with exit code {returncode}")
    if "--json" not in command:
        return None, combined
    try:
        return json.loads(output), combined
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", output, flags=re.S)
        if match:
            return json.loads(match.group(1)), combined
        raise RuntimeError(f"Expected JSON output, got: {combined}") from None


def extract_notebook_id(create_response: dict[str, Any] | None) -> str | None:
    if not create_response:
        return None
    direct_id = create_response.get("id")
    if direct_id:
        return str(direct_id)
    notebook = create_response.get("notebook")
    if isinstance(notebook, dict) and notebook.get("id"):
        return str(notebook["id"])
    return None


def find_existing_notebook_id(
    notebooklm_bin: str,
    title: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> str | None:
    listed, _ = run_command([notebooklm_bin, "list", "--json"], env=env, cwd=cwd)
    for notebook in (listed or {}).get("notebooks", []):
        if notebook.get("title") == title and notebook.get("id"):
            return str(notebook["id"])
    return None


def extract_source_id(source_response: dict[str, Any] | None) -> str | None:
    if not source_response:
        return None
    for key in ("source_id", "id"):
        if source_response.get(key):
            return str(source_response[key])
    source = source_response.get("source")
    if isinstance(source, dict):
        for key in ("source_id", "id"):
            if source.get(key):
                return str(source[key])
    return None


def find_existing_source_id(
    notebooklm_bin: str,
    notebook_id: str,
    title: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> str | None:
    listed, _ = run_command(
        [notebooklm_bin, "source", "list", "-n", notebook_id, "--json"],
        env=env,
        cwd=cwd,
    )
    for source in (listed or {}).get("sources", []):
        if source.get("title") == title and source.get("id"):
            return str(source["id"])
    return None


def find_existing_artifact_id(
    notebooklm_bin: str,
    notebook_id: str,
    title: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> str | None:
    listed, _ = run_command(
        [notebooklm_bin, "artifact", "list", "-n", notebook_id, "--json"],
        env=env,
        cwd=cwd,
        timeout=120,
    )
    for artifact in (listed or {}).get("artifacts", []):
        if artifact.get("title") == title and artifact.get("id"):
            return str(artifact["id"])
    return None


def notebook_title(prefix: str, book_title: str, chapter: Chapter) -> str:
    if not prefix.strip():
        return safe_title(f"{chapter.title} | {chapter.label} | {book_title}", max_len=140)
    return safe_title(f"{prefix}: {book_title} | {chapter.label} - {chapter.title}", max_len=140)


def artifact_title(chapter: Chapter, suffix: str) -> str:
    return safe_title(f"{chapter.label} - {suffix} - {chapter.title}", max_len=140)


def prompt_with_context(base: str, prompt_context: str | None) -> str:
    if not prompt_context:
        return base
    return f"{base}\n\nAdditional context for this book: {prompt_context.strip()}"


def build_artifact_specs(
    notebooklm_bin: str,
    notebook_id: str,
    full_source_id: str,
    part_source_ids: list[str],
    chapter: Chapter,
    artifacts_dir: Path,
    language: str,
    retry: int,
    wait_artifacts: bool,
    prompt_context: str | None,
) -> list[ArtifactRun]:
    chapter_artifacts = artifacts_dir / f"chapter-{chapter.output_index:02d}-{chapter.slug}"
    chapter_artifacts.mkdir(parents=True, exist_ok=True)

    audio_title = artifact_title(chapter, "Audio Deep Dive")
    slide_title = artifact_title(chapter, "Practical Slide Deck")
    infographic_title = artifact_title(chapter, "Practical Infographic")

    specs = [
        ArtifactRun(
            kind="audio",
            title=audio_title,
            source_files=[chapter.chapter_file],
            command=[
                notebooklm_bin,
                "generate",
                "audio",
                "-n",
                notebook_id,
                "-s",
                full_source_id,
                "--format",
                "deep-dive",
                "--length",
                "long",
                "--language",
                language,
                "--wait" if wait_artifacts else "--no-wait",
                "--retry",
                str(retry),
                "--json",
                prompt_with_context(
                    "In English. Create a long practical audio deep dive for this chapter. "
                    "Explain the core concepts, causal relationships, examples, decision-making uses, "
                    "actionable takeaways, weak spots in the argument, common misuse cases, and reflection questions.",
                    prompt_context,
                ),
            ],
            output_path=str(chapter_artifacts / "audio-podcast.mp3"),
        ),
        ArtifactRun(
            kind="slide-deck",
            title=slide_title,
            source_files=[chapter.chapter_file],
            command=[
                notebooklm_bin,
                "generate",
                "slide-deck",
                "-n",
                notebook_id,
                "-s",
                full_source_id,
                "--format",
                "detailed",
                "--length",
                "default",
                "--language",
                language,
                "--wait" if wait_artifacts else "--no-wait",
                "--retry",
                str(retry),
                "--json",
                prompt_with_context(
                    "In English. Create a detailed practical learning slide deck for this chapter: "
                    "structure, key concepts, argument logic, examples, practical decision checklists, "
                    "comparison points, exercises, conclusions, and discussion questions.",
                    prompt_context,
                ),
            ],
            output_path=str(chapter_artifacts / "presentation.pptx"),
        ),
        ArtifactRun(
            kind="infographic",
            title=infographic_title,
            source_files=[chapter.chapter_file],
            command=[
                notebooklm_bin,
                "generate",
                "infographic",
                "-n",
                notebook_id,
                "-s",
                full_source_id,
                "--orientation",
                "portrait",
                "--detail",
                "detailed",
                "--style",
                "scientific",
                "--language",
                language,
                "--wait" if wait_artifacts else "--no-wait",
                "--retry",
                str(retry),
                "--json",
                prompt_with_context(
                    "In English. Create an infographic that maps the chapter for practical use: "
                    "main concepts, relationships, processes, examples, action steps, and warnings.",
                    prompt_context,
                ),
            ],
            output_path=str(chapter_artifacts / "infographic.png"),
        ),
    ]

    for index, source_id in enumerate(part_source_ids, start=1):
        title = artifact_title(chapter, f"Quiz {index} - Part {index}")
        specs.append(
            ArtifactRun(
                kind="quiz",
                title=title,
                source_files=[chapter.part_files[index - 1]],
                command=[
                    notebooklm_bin,
                    "generate",
                    "quiz",
                    "-n",
                    notebook_id,
                    "-s",
                    source_id,
                    "--quantity",
                    "more",
                    "--difficulty",
                    "hard",
                    "--wait" if wait_artifacts else "--no-wait",
                    "--retry",
                    str(retry),
                    "--json",
                    prompt_with_context(
                        f"In English. Create a difficult learning quiz for {chapter.label}, "
                        f"part {index}. Cover concepts, causal relationships, details, practical applications, "
                        "edge cases, and common misunderstanding traps.",
                        prompt_context,
                    ),
                ],
                output_path=str(chapter_artifacts / f"quiz-{index:02d}.md"),
            )
        )
    return specs


def download_artifact(
    notebooklm_bin: str,
    notebook_id: str,
    run: ArtifactRun,
    *,
    env: dict[str, str],
    cwd: Path,
) -> None:
    if not run.artifact_id or not run.output_path:
        return
    output_path = Path(run.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if run.kind == "audio":
        command = [notebooklm_bin, "download", "audio", "-n", notebook_id, "-a", run.artifact_id, str(output_path), "--force"]
    elif run.kind == "slide-deck":
        command = [
            notebooklm_bin,
            "download",
            "slide-deck",
            "-n",
            notebook_id,
            "-a",
            run.artifact_id,
            "--format",
            "pptx",
            str(output_path),
            "--force",
        ]
    elif run.kind == "quiz":
        command = [
            notebooklm_bin,
            "download",
            "quiz",
            "-n",
            notebook_id,
            "-a",
            run.artifact_id,
            "--format",
            "markdown",
            str(output_path),
        ]
    elif run.kind == "infographic":
        command = [
            notebooklm_bin,
            "download",
            "infographic",
            "-n",
            notebook_id,
            "-a",
            run.artifact_id,
            str(output_path),
            "--force",
        ]
    else:
        return
    run_command(command, env=env, cwd=cwd)


def execute_chapter(
    chapter: Chapter,
    *,
    notebooklm_bin: str,
    notebook_prefix: str,
    book_title: str,
    language: str,
    artifacts_dir: Path,
    cwd: Path,
    source_timeout: int,
    retry: int,
    skip_download: bool,
    wait_artifacts: bool,
    prompt_context: str | None,
    artifact_timeout: int,
) -> dict[str, Any]:
    env = command_env(notebooklm_bin)
    nb_title = notebook_title(notebook_prefix, book_title, chapter)
    notebook_id = find_existing_notebook_id(notebooklm_bin, nb_title, env=env, cwd=cwd)
    if not notebook_id:
        create_json, _ = run_command([notebooklm_bin, "create", nb_title, "--json"], env=env, cwd=cwd)
        notebook_id = extract_notebook_id(create_json)
    if not notebook_id:
        raise RuntimeError(f"Notebook creation did not return an id: {create_json}")

    full_title = f"{chapter.label} - Full Chapter Source - {chapter.title}"
    full_source_id = find_existing_source_id(notebooklm_bin, notebook_id, full_title, env=env, cwd=cwd)
    if not full_source_id:
        source_json, _ = run_command(
            [
                notebooklm_bin,
                "source",
                "add",
                "-n",
                notebook_id,
                chapter.chapter_file,
                "--type",
                "text",
                "--title",
                full_title,
                "--json",
            ],
            env=env,
            cwd=cwd,
        )
        full_source_id = extract_source_id(source_json)
    if not full_source_id:
        raise RuntimeError(f"Source add did not return an id for {full_title}")
    run_command(
        [notebooklm_bin, "source", "wait", "-n", notebook_id, full_source_id, "--timeout", str(source_timeout), "--json"],
        env=env,
        cwd=cwd,
        timeout=source_timeout + 30,
    )

    part_source_ids: list[str] = []
    for index, part_file in enumerate(chapter.part_files, start=1):
        part_title = f"{chapter.label} - Part {index} - {chapter.title}"
        part_source_id = find_existing_source_id(notebooklm_bin, notebook_id, part_title, env=env, cwd=cwd)
        if not part_source_id:
            part_json, _ = run_command(
                [
                    notebooklm_bin,
                    "source",
                    "add",
                    "-n",
                    notebook_id,
                    part_file,
                    "--type",
                    "text",
                    "--title",
                    part_title,
                    "--json",
                ],
                env=env,
                cwd=cwd,
            )
            part_source_id = extract_source_id(part_json)
        if not part_source_id:
            raise RuntimeError(f"Source add did not return an id for {part_title}")
        part_source_ids.append(part_source_id)
        run_command(
            [notebooklm_bin, "source", "wait", "-n", notebook_id, part_source_id, "--timeout", str(source_timeout), "--json"],
            env=env,
            cwd=cwd,
            timeout=source_timeout + 30,
        )

    artifacts = build_artifact_specs(
        notebooklm_bin=notebooklm_bin,
        notebook_id=notebook_id,
        full_source_id=full_source_id,
        part_source_ids=part_source_ids,
        chapter=chapter,
        artifacts_dir=artifacts_dir,
        language=language,
        retry=retry,
        wait_artifacts=wait_artifacts,
        prompt_context=prompt_context,
    )

    for artifact in artifacts:
        try:
            existing_artifact_id = find_existing_artifact_id(
                notebooklm_bin,
                notebook_id,
                artifact.title,
                env=env,
                cwd=cwd,
            )
            if existing_artifact_id:
                artifact.artifact_id = existing_artifact_id
                artifact.status = "existing"
                continue

            generated, _ = run_command(artifact.command, env=env, cwd=cwd, timeout=artifact_timeout)
            artifact.artifact_id = str((generated or {}).get("task_id") or (generated or {}).get("artifact_id") or "")
            if artifact.artifact_id:
                run_command(
                    [notebooklm_bin, "artifact", "rename", "-n", notebook_id, artifact.artifact_id, artifact.title],
                    env=env,
                    cwd=cwd,
                )
                if not skip_download:
                    download_artifact(notebooklm_bin, notebook_id, artifact, env=env, cwd=cwd)
            artifact.status = "completed"
        except Exception as exc:  # Keep later artifacts moving after quota/API failures.
            artifact.status = "failed"
            artifact.error = str(exc)

    return {
        "id": notebook_id,
        "title": nb_title,
        "sources": {
            "full_chapter": full_source_id,
            "parts": part_source_ids,
        },
        "artifacts": [asdict(artifact) for artifact in artifacts],
    }


def write_plan(
    run_dir: Path,
    *,
    book_title: str,
    chapters: list[Chapter],
    notebooklm_bin: str,
    language: str,
    mode: str,
) -> None:
    lines = [
        f"# NotebookLM Book Pipeline Plan: {book_title}",
        "",
        f"- Mode: `{mode}`",
        f"- Language: `{language}`",
        f"- NotebookLM CLI: `{notebooklm_bin}`",
        f"- Chapters retained: `{len(chapters)}`",
        "",
        "## Artifact Naming",
        "",
        "- `Chapter NN - Audio Deep Dive - <chapter title>`",
        "- `Chapter NN - Practical Slide Deck - <chapter title>`",
        "- `Chapter NN - Quiz 1 - Part 1 - <chapter title>`",
        "- `Chapter NN - Quiz 2 - Part 2 - <chapter title>`",
        "- `Chapter NN - Quiz 3 - Part 3 - <chapter title>`",
        "- `Chapter NN - Practical Infographic - <chapter title>`",
        "",
        "## Chapters",
        "",
    ]
    for chapter in chapters:
        lines.extend(
            [
                f"### {chapter.label}: {chapter.title}",
                "",
                f"- Full source: `{chapter.chapter_file}`",
                f"- Part sources: `{', '.join(chapter.part_files)}`",
                "",
            ]
        )
    (run_dir / "notebooklm_plan.md").write_text("\n".join(lines), encoding="utf-8")


def create_manifest(
    *,
    book_path: Path,
    book_title: str,
    run_dir: Path,
    mode: str,
    split_strategy: str,
    notebooklm_bin: str,
    chapters: list[Chapter],
    skipped: list[SkippedSection],
) -> dict[str, Any]:
    return {
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": mode,
        "book": {
            "path": str(book_path),
            "title": book_title,
        },
        "run_dir": str(run_dir),
        "split_strategy": split_strategy,
        "notebooklm_bin": notebooklm_bin,
        "chapters": [asdict(chapter) for chapter in chapters],
        "skipped_sections": [asdict(section) for section in skipped],
        "notebooklm_runs": [],
    }


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    (run_dir / "book_pipeline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", required=True, help="Path to the book file (.pdf, .epub, .docx, .md, .txt, .rtf).")
    parser.add_argument("--title", default=None, help="Book title for notebook naming.")
    parser.add_argument("--mode", choices=("prepare", "execute"), default="prepare", help="prepare writes chapters/plan only; execute runs NotebookLM.")
    parser.add_argument("--output-root", default="outputs/book_pipeline", help="Root directory for run outputs.")
    parser.add_argument("--language", default="uk", help="NotebookLM artifact language code.")
    parser.add_argument("--notebook-prefix", default="Book Pipeline", help="Prefix for created NotebookLM notebook titles.")
    parser.add_argument("--prompt-context", default=None, help="Extra book-specific guidance appended to every generation prompt.")
    parser.add_argument("--notebooklm-bin", default=None, help="Path to notebooklm CLI binary.")
    parser.add_argument("--keep-intro", action="store_true", help="Keep introduction/вступ sections as content chapters.")
    parser.add_argument("--min-chapter-chars", type=int, default=2500, help="Drop detected sections shorter than this.")
    parser.add_argument("--target-chapter-chars", type=int, default=45000, help="Fallback chunk size when headings are not detected.")
    parser.add_argument("--part-count", type=int, default=3, help="Number of text parts per chapter for quizzes.")
    parser.add_argument("--start-chapter", type=int, default=None, help="Start from this retained chapter number, 1-based.")
    parser.add_argument("--end-chapter", type=int, default=None, help="Stop at this retained chapter number, 1-based inclusive.")
    parser.add_argument("--max-chapters", type=int, default=None, help="Limit retained chapters for testing.")
    parser.add_argument("--source-timeout", type=int, default=300, help="Seconds to wait for each source.")
    parser.add_argument("--retry", type=int, default=2, help="NotebookLM generation retry count.")
    parser.add_argument("--wait-artifacts", action="store_true", help="Wait for each artifact to finish before continuing. Slower but stricter.")
    parser.add_argument("--artifact-timeout", type=int, default=180, help="Seconds to wait for each NotebookLM artifact command to return.")
    parser.add_argument("--skip-download", action="store_true", help="Generate and rename artifacts but do not download them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    book_path = Path(args.book).expanduser().resolve()
    if not book_path.exists():
        parser.error(f"Book file not found: {book_path}")

    book_title = safe_title(args.title or book_path.stem, max_len=100)
    notebooklm_bin = resolve_notebooklm_bin(args.notebooklm_bin)
    raw_text = extract_book_text(book_path)
    normalized = normalize_text(raw_text)
    chapters, skipped, split_strategy = split_chapters(
        normalized,
        keep_intro=args.keep_intro,
        min_chapter_chars=args.min_chapter_chars,
        target_chapter_chars=args.target_chapter_chars,
    )
    if args.max_chapters:
        chapters = chapters[: args.max_chapters]
    if args.start_chapter or args.end_chapter:
        start_index = max(0, (args.start_chapter or 1) - 1)
        end_index = args.end_chapter if args.end_chapter else None
        chapters = chapters[start_index:end_index]
        for output_index, chapter in enumerate(chapters, start=start_index + 1):
            chapter.output_index = output_index
    if not chapters:
        raise RuntimeError("No content chapters were detected. Try --keep-intro or lower --min-chapter-chars.")

    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.output_root).expanduser().resolve() / slugify(book_title) / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    write_chapter_files(run_dir, chapters, part_count=args.part_count)

    manifest = create_manifest(
        book_path=book_path,
        book_title=book_title,
        run_dir=run_dir,
        mode=args.mode,
        split_strategy=split_strategy,
        notebooklm_bin=notebooklm_bin,
        chapters=chapters,
        skipped=skipped,
    )
    write_plan(
        run_dir,
        book_title=book_title,
        chapters=chapters,
        notebooklm_bin=notebooklm_bin,
        language=args.language,
        mode=args.mode,
    )
    write_manifest(run_dir, manifest)

    if args.mode == "execute":
        artifacts_dir = run_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for chapter in chapters:
            chapter_run: dict[str, Any] = {
                "chapter_index": chapter.output_index,
                "chapter_title": chapter.title,
                "status": "pending",
            }
            try:
                chapter_run.update(
                    execute_chapter(
                        chapter,
                        notebooklm_bin=notebooklm_bin,
                        notebook_prefix=args.notebook_prefix,
                        book_title=book_title,
                        language=args.language,
                        artifacts_dir=artifacts_dir,
                        cwd=Path.cwd(),
                        source_timeout=args.source_timeout,
                        retry=args.retry,
                        skip_download=args.skip_download,
                        wait_artifacts=args.wait_artifacts,
                        prompt_context=args.prompt_context,
                        artifact_timeout=args.artifact_timeout,
                    )
                )
                chapter_run["status"] = "completed"
            except Exception as exc:
                chapter_run["status"] = "failed"
                chapter_run["error"] = str(exc)
            manifest["notebooklm_runs"].append(chapter_run)
            write_manifest(run_dir, manifest)

    print(json.dumps({"run_dir": str(run_dir), "chapters": len(chapters), "mode": args.mode}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
