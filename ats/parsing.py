"""Resume text extraction for PDF, DOCX, TXT/MD and ZIP archives of those."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}
MIN_TEXT_CHARS = 200  # below this (and no page images) we assume a broken file
MAX_IMAGE_PAGES = 4  # cap on page images sent per resume (cost)
IMAGE_AREA_FRAC = 0.05  # a picture covering more than this share of a page counts as content, not a logo
IMAGE_LONG_EDGE = 1568  # px; larger images are downscaled by the API anyway


@dataclass
class Resume:
    filename: str
    text: str
    sha256: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw: bytes = field(default=b"", repr=False)  # original file bytes, so the UI can open the resume
    images: list[bytes] = field(default_factory=list, repr=False)  # JPEG renders of pages with scans/pictures, sent to the LLM

    @property
    def ok(self) -> bool:
        return self.error is None


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _pdf_text(data: bytes) -> str:
    from pdf_inspector import process_pdf_bytes

    md = process_pdf_bytes(data).markdown
    if md:
        return md
    # pdf-inspector returns nothing for pages it judges image-based, even when they carry some real text
    # (e.g. text plus a big picture). Fall back to the raw text layer; a true scan yields "" and the page
    # images (see _pdf_images) carry the content to the LLM.
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        return "\n\n".join(p.get_textpage().get_text_bounded() for p in pdf)
    finally:
        pdf.close()


def _pdf_images(data: bytes) -> list[bytes]:
    """JPEG renders of pages that are scans (almost no text) or hold a sizeable picture (e.g. a table or skills
    chart saved as an image), so the LLM can read what text extraction misses."""
    import pypdfium2 as pdfium
    from pypdfium2 import raw as pdfium_c

    out: list[bytes] = []
    pdf = pdfium.PdfDocument(data)
    try:
        for page in pdf:
            if len(out) >= MAX_IMAGE_PAGES:
                break
            w, h = page.get_size()
            big = any((r - l) * (t - b) > IMAGE_AREA_FRAC * w * h
                      for l, b, r, t in (o.get_bounds() for o in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE])))
            if big or page.get_textpage().count_chars() < 40:
                img = page.render(scale=min(2.0, IMAGE_LONG_EDGE / max(w, h))).to_pil().convert("RGB")
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=80)
                out.append(buf.getvalue())
    finally:
        pdf.close()
    return out


def _md_table(table) -> str:
    rows = []
    for row in table.rows:
        cells, last = [], None
        for c in row.cells:
            if c._tc is last:  # merged cells repeat the same cell object; keep one
                continue
            last = c._tc
            cells.append(" ".join(c.text.split()).replace("|", "\\|"))
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    n = max(map(len, rows))
    rows = [r + [""] * (n - len(r)) for r in rows]
    lines = ["| " + " | ".join(r) + " |" for r in rows]
    lines.insert(1, "|" + " --- |" * n)  # first row is used as the header
    return "\n".join(lines)


def _docx_text(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(io.BytesIO(data))
    parts = []
    for el in doc.element.body.iterchildren():  # document order, so tables stay under their headings
        if el.tag.endswith("}p"):
            parts.append(Paragraph(el, doc).text)
        elif el.tag.endswith("}tbl"):
            parts.append("\n" + _md_table(Table(el, doc)) + "\n")
    return "\n".join(parts)


def extract_text(filename: str, data: bytes) -> Resume:
    ext = PurePosixPath(filename).suffix.lower()
    sha = hashlib.sha256(data).hexdigest()
    images: list[bytes] = []
    try:
        if ext == ".pdf":
            text = _pdf_text(data)
            try:
                images = _pdf_images(data)
            except Exception:  # noqa: BLE001  rendering is best-effort; text extraction still stands
                images = []
        elif ext == ".docx":
            text = _docx_text(data)
        elif ext in {".txt", ".md"}:
            text = data.decode("utf-8", errors="replace")
        else:
            return Resume(filename, "", sha, error=f"Unsupported file type '{ext}'")
    except Exception as exc:  # noqa: BLE001
        return Resume(filename, "", sha, error=f"Could not read file: {exc}")

    text = _clean(text)
    # Fingerprint ignores case/whitespace/punctuation so the same resume as PDF and DOCX counts as a duplicate.
    norm = re.sub(r"\W+", "", text.lower())
    res = Resume(filename, text, hashlib.sha256(norm.encode()).hexdigest() if len(text) >= MIN_TEXT_CHARS else sha, images=images)
    if len(text) < MIN_TEXT_CHARS and not images:
        res.error = "Little or no text extracted (scanned/image PDF?). Needs OCR or manual review."
    return res


def load_files(files: list[tuple[str, bytes]]) -> list[Resume]:
    """Accepts (filename, bytes) pairs; ZIPs are expanded (nested folders ok)."""
    out: list[Resume] = []
    for name, data in files:
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        inner = PurePosixPath(info.filename)
                        if info.is_dir() or inner.name.startswith((".", "~$")) or "__MACOSX" in inner.parts:
                            continue
                        if inner.suffix.lower() in SUPPORTED_EXT:
                            data_in = zf.read(info)
                            out.append(replace(extract_text(inner.name, data_in), raw=data_in))
            except zipfile.BadZipFile:
                out.append(Resume(name, "", "", error="Corrupt ZIP archive"))
        else:
            out.append(replace(extract_text(name, data), raw=data))
    return out


def dedupe(resumes: list[Resume]) -> tuple[list[Resume], list[Resume]]:
    """Splits into (unique, duplicates) by extracted-text hash."""
    seen: set[str] = set()
    unique, dups = [], []
    for r in resumes:
        if r.ok and r.sha256 in seen:
            dups.append(r)
        else:
            if r.ok:
                seen.add(r.sha256)
            unique.append(r)
    return unique, dups
