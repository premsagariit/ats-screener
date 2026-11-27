"""Resume text extraction for PDF, DOCX, TXT/MD and ZIP archives of those."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}
MIN_TEXT_CHARS = 200  # below this we assume a scanned/image PDF or a broken file


@dataclass
class Resume:
    filename: str
    text: str
    sha256: str
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

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

    res = process_pdf_bytes(data)
    if not res.markdown:  # scanned/image PDF: no text layer, pdf-inspector does no OCR
        raise ValueError(f"no extractable text (pdf_type={res.pdf_type})")
    return res.markdown


def _docx_text(data: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:  # many resume templates put content in tables
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def extract_text(filename: str, data: bytes) -> Resume:
    ext = PurePosixPath(filename).suffix.lower()
    sha = hashlib.sha256(data).hexdigest()
    try:
        if ext == ".pdf":
            text = _pdf_text(data)
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
    res = Resume(filename, text, hashlib.sha256(norm.encode()).hexdigest() if norm else sha)
    if len(text) < MIN_TEXT_CHARS:
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
                            out.append(extract_text(inner.name, zf.read(info)))
            except zipfile.BadZipFile:
                out.append(Resume(name, "", "", error="Corrupt ZIP archive"))
        else:
            out.append(extract_text(name, data))
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
