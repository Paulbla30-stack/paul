"""Reading the formats a person actually hands over, and saying what was missed.

read_file could read text. Everything else came back "this looks like a binary
file", which covers a bill, a statement, a letter, a spreadsheet and a
photograph -- most of what anyone would think to upload. A capability that
only accepts the format nobody sends is not a capability.

The harder half is not extraction, it is honesty about extraction. A scanned
PDF has no text layer: every naive reader returns an empty string for it, and
an empty string is indistinguishable from a blank document. This codebase has
met that failure before -- an empty scan report being read as a report of zero
findings -- and the rule it settled on holds here. **What could not be read is
reported as not read, never as nothing there.** So every extractor returns
``complete``, and when that is False it says what was missed and why, in words
the model is meant to repeat rather than paraphrase away.

Format is decided by magic bytes before extension, because an extension is a
claim by whoever named the file and the first four bytes are a fact about it.

Everything here is stdlib except PDF. The Office formats are ZIP containers
full of XML, which zipfile and a careful regex handle without a dependency --
and a dependency on this box is a dependency in the AMI, in the build, and in
every future image, so they are worth not taking. PDF genuinely is not
tractable that way (compressed streams, font encodings, no plain text
anywhere), so it uses pypdf when it is installed and says plainly that it
cannot when it is not. An absent reader is a fact about this machine, and
reporting it is how the operator learns to install one.

Images are not read here at all, on purpose. Extracting text from a photograph
is optical character recognition or a vision model, both of which are real
decisions with real costs, and neither is something to slip in behind a
function called read. An image is identified, measured, and declined with the
reason.
"""

import io
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional

# Bounds. A document is not a log, so these are larger than the plain-text
# read, but a model handed forty pages will still cost more than it returns.
MAX_TEXT_CHARS = 60_000
MAX_PDF_PAGES = 40
MAX_SHEET_ROWS = 500
MAX_SLIDES = 60

TEXT = "text"
PDF = "pdf"
DOCX = "docx"
XLSX = "xlsx"
PPTX = "pptx"
IMAGE = "image"
ZIP = "zip"
UNKNOWN = "unknown"

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff(path: str, head: bytes = b"") -> str:
    """What this file actually is, from its bytes before its name."""
    if head.startswith(b"%PDF-"):
        return PDF
    for magic, _ in _IMAGE_MAGIC:
        if head.startswith(magic):
            return IMAGE
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return IMAGE
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path)
    return TEXT


def _zip_kind(path: str) -> str:
    """An Office file is a zip with a known member inside it."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except Exception:
        return ZIP
    if "word/document.xml" in names:
        return DOCX
    if "xl/workbook.xml" in names:
        return XLSX
    if "ppt/presentation.xml" in names:
        return PPTX
    return ZIP


def image_size(head: bytes) -> Optional[tuple]:
    """Width and height from a header, so an image can at least be described."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            return struct.unpack(">II", head[16:24])
        if head.startswith(b"GIF8") and len(head) >= 10:
            return struct.unpack("<HH", head[6:10])
        if head.startswith(b"BM") and len(head) >= 26:
            return struct.unpack("<ii", head[18:26])
        if head.startswith(b"\xff\xd8\xff"):
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    return (w, h)
                i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    except Exception:
        return None
    return None


# --- extractors -------------------------------------------------------------
#
# Each returns {"text", "complete", "note"} plus whatever counts make sense for
# the format. ``complete`` False with no note is a bug: the whole point is that
# a gap is named.

def _clip(text: str) -> tuple:
    if len(text) <= MAX_TEXT_CHARS:
        return text, True, ""
    return (text[:MAX_TEXT_CHARS], False,
            f"only the first {MAX_TEXT_CHARS:,} characters are shown; "
            f"the document continues past them and has not been read")


def read_pdf(path: str) -> dict:
    """Text per page, and which pages had none.

    A page with no text layer is a scan. Returning "" for it and stopping
    would tell the reader the document is blank, which is the one thing that
    must not happen here.
    """
    try:
        import pypdf
    except ImportError:
        return {"format": PDF, "text": "", "complete": False,
                "note": ("this is a PDF and no PDF reader is installed on this "
                         "machine, so its contents have not been read; "
                         "`pip install pypdf` would fix that")}
    try:
        reader = pypdf.PdfReader(path)
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return {"format": PDF, "text": "", "complete": False,
                        "note": "this PDF is password protected and has not been read"}
        total = len(reader.pages)
        chunks, blank = [], []
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            try:
                body = (page.extract_text() or "").strip()
            except Exception:
                body = ""
            if body:
                chunks.append(f"[page {i}]\n{body}")
            else:
                blank.append(i)
    except Exception as exc:
        return {"format": PDF, "text": "", "complete": False,
                "note": f"this PDF could not be parsed: {type(exc).__name__}"}

    text, fit, clip_note = _clip("\n\n".join(chunks))
    notes = [clip_note] if clip_note else []
    if total > MAX_PDF_PAGES:
        notes.append(f"only the first {MAX_PDF_PAGES} of {total} pages were looked at")
    if blank:
        shown = ", ".join(str(n) for n in blank[:10])
        more = f" (+{len(blank) - 10} more)" if len(blank) > 10 else ""
        if len(blank) == min(total, MAX_PDF_PAGES):
            notes.append("no page in this PDF has a text layer, so it is very "
                         "likely a scan or photographs; nothing in it has been "
                         "read and it is not blank")
        else:
            notes.append(f"page(s) {shown}{more} have no text layer (likely scanned); "
                         "their contents have not been read")
    return {"format": PDF, "text": text, "pages": total,
            "pages_read": len(chunks), "pages_without_text": len(blank),
            "complete": fit and not blank and total <= MAX_PDF_PAGES,
            "note": "; ".join(notes)}


def _xml_text(blob: bytes, tag: str, break_tag: Optional[str] = None) -> str:
    """Text runs out of an Office XML part, with breaks where the format has them."""
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return ""
    out, current = [], []
    for el in root.iter():
        name = el.tag.rsplit("}", 1)[-1]
        if name == tag and el.text:
            current.append(el.text)
        elif break_tag and name == break_tag:
            if current:
                out.append("".join(current))
                current = []
    if current:
        out.append("".join(current))
    return "\n".join(out)


def read_docx(path: str) -> dict:
    """A Word document is a zip; the body is word/document.xml."""
    try:
        with zipfile.ZipFile(path) as z:
            body = _xml_text(z.read("word/document.xml"), "t", "p")
            extras = [n for n in z.namelist()
                      if n.startswith("word/media/") or n.endswith("/footnotes.xml")]
    except Exception as exc:
        return {"format": DOCX, "text": "", "complete": False,
                "note": f"this .docx could not be opened: {type(exc).__name__}"}
    text, fit, clip_note = _clip(body.strip())
    notes = [clip_note] if clip_note else []
    images = [n for n in extras if n.startswith("word/media/")]
    if images:
        notes.append(f"it also contains {len(images)} embedded image(s), "
                     "which have not been read")
    if not text:
        notes.append("no text was found in this document; it may be empty or "
                     "may hold only images")
    return {"format": DOCX, "text": text,
            "complete": fit and not images and bool(text),
            "note": "; ".join(notes)}


def read_pptx(path: str) -> dict:
    """Slides in order, text runs only."""
    try:
        with zipfile.ZipFile(path) as z:
            slides = sorted(n for n in z.namelist()
                            if re.match(r"ppt/slides/slide\d+\.xml$", n))
            chunks = []
            for i, name in enumerate(slides[:MAX_SLIDES], start=1):
                body = _xml_text(z.read(name), "t", "p").strip()
                chunks.append(f"[slide {i}]\n{body}" if body
                              else f"[slide {i}] (no text)")
    except Exception as exc:
        return {"format": PPTX, "text": "", "complete": False,
                "note": f"this .pptx could not be opened: {type(exc).__name__}"}
    text, fit, clip_note = _clip("\n\n".join(chunks))
    notes = [clip_note] if clip_note else []
    if len(slides) > MAX_SLIDES:
        notes.append(f"only the first {MAX_SLIDES} of {len(slides)} slides were read")
    notes.append("speaker notes and images have not been read")
    return {"format": PPTX, "text": text, "slides": len(slides),
            "complete": False, "note": "; ".join(notes)}


def read_xlsx(path: str) -> dict:
    """Cells as rows of tab-separated values, with the formulas left behind.

    A spreadsheet's stored value is the last one its application calculated.
    Reading cached values rather than recomputing formulas is the honest
    thing, and saying so matters: a figure here is what Excel last wrote, not
    something this machine worked out.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                try:
                    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    for si in root:
                        shared.append("".join(
                            t.text or "" for t in si.iter()
                            if t.tag.rsplit("}", 1)[-1] == "t"))
                except ET.ParseError:
                    shared = []
            sheets = sorted(n for n in names
                            if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
            blocks, truncated_sheets = [], []
            for si, name in enumerate(sheets, start=1):
                rows = _sheet_rows(z.read(name), shared)
                if len(rows) > MAX_SHEET_ROWS:
                    truncated_sheets.append((si, len(rows)))
                    rows = rows[:MAX_SHEET_ROWS]
                body = "\n".join("\t".join(r) for r in rows)
                blocks.append(f"[sheet {si}]\n{body}" if body
                              else f"[sheet {si}] (empty)")
    except Exception as exc:
        return {"format": XLSX, "text": "", "complete": False,
                "note": f"this .xlsx could not be opened: {type(exc).__name__}"}
    text, fit, clip_note = _clip("\n\n".join(blocks))
    notes = [clip_note] if clip_note else []
    for si, total in truncated_sheets:
        notes.append(f"sheet {si} has {total} rows and only the first "
                     f"{MAX_SHEET_ROWS} were read")
    notes.append("these are the values the spreadsheet last saved, not "
                 "recalculated formulas, and charts have not been read")
    return {"format": XLSX, "text": text, "sheets": len(sheets),
            "complete": fit and not truncated_sheets, "note": "; ".join(notes)}


def _sheet_rows(blob: bytes, shared: list) -> list:
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    rows = []
    for row in root.iter():
        if row.tag.rsplit("}", 1)[-1] != "row":
            continue
        cells = []
        for c in row:
            kind = c.get("t")
            value = ""
            for child in c:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "v":
                    value = child.text or ""
                elif tag == "is":
                    value = "".join(t.text or "" for t in child.iter()
                                    if t.tag.rsplit("}", 1)[-1] == "t")
            if kind == "s":
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    value = ""
            cells.append(value)
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells)
    return rows


def read_image(path: str, head: bytes) -> dict:
    """Identified and declined, with the reason.

    Pulling text out of a photograph is OCR or a vision model. Both are real
    decisions with real costs, and neither belongs behind a function called
    read. What can be said truthfully is what the file is and how big it is.
    """
    size = image_size(head)
    kind = next((name for magic, name in _IMAGE_MAGIC if head.startswith(magic)), "image")
    where = f"{size[0]}x{size[1]} " if size else ""
    return {"format": IMAGE, "text": "", "complete": False,
            "image": {"kind": kind, "width": size[0] if size else None,
                      "height": size[1] if size else None},
            "note": (f"this is a {where}{kind} image. Nothing on this machine can "
                     "read what it shows: that needs optical character "
                     "recognition or a model that sees, and neither is "
                     "configured. Its contents are unknown, not empty.")}


def extract(path: str, head: bytes, kind: Optional[str] = None) -> Optional[dict]:
    """Pull text out of a non-plain-text file, or None if it is plain text.

    Never raises. A format this cannot handle comes back saying so, because a
    reader that throws teaches the caller to stop asking.
    """
    kind = kind or sniff(path, head)
    try:
        if kind == PDF:
            return read_pdf(path)
        if kind == DOCX:
            return read_docx(path)
        if kind == XLSX:
            return read_xlsx(path)
        if kind == PPTX:
            return read_pptx(path)
        if kind == IMAGE:
            return read_image(path, head)
        if kind == ZIP:
            return {"format": ZIP, "text": "", "complete": False,
                    "note": ("this is a zip archive; its contents have not been "
                             "read and would need extracting first")}
    except Exception as exc:                      # never the caller's problem
        return {"format": kind, "text": "", "complete": False,
                "note": f"this file could not be read: {type(exc).__name__}"}
    return None


def summary(result: dict) -> str:
    """One line saying what was read and what was not. For the operator."""
    if not result:
        return ""
    kind = result.get("format", "file")
    if result.get("complete"):
        return f"read the whole {kind}"
    note = result.get("note") or "part of it could not be read"
    return f"{kind}: {note}"
