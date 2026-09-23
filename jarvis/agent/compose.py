"""Writing the formats a person actually reads: .docx, .pdf, .md, .txt.

``documents.py`` reads what the operator hands over. This is the other
direction, and it was missing: the agent could read a statement and could not
produce a letter, a summary or a report that anyone could open. "I have
written it out below" followed by four hundred words in a chat window is not a
document -- it cannot be printed, attached, filed or signed, which is most of
what a document is for.

**Stdlib only, for the same reason the reader is.** A dependency here is a
dependency in the AMI, in the build and in every future image. Word files are
ZIP containers full of XML, which ``zipfile`` and a few templates handle
outright. PDF is the interesting case: ``documents.py`` needs ``pypdf`` to
*read* one, and this needs nothing to *write* one, which is not a
contradiction. Reading an arbitrary PDF means compressed streams, arbitrary
font encodings and text with no reading order. Writing one means emitting a
handful of objects and a cross-reference table, in a format whose whole design
is that a producer can be simple.

**What it deliberately does not do.** No images, no nested tables, no merged
cells, no styling beyond headings, paragraphs, bullets, rules and a plain
table. Every one of those is a real feature and each is a place for the layout
to be subtly wrong in a file the operator sends to somebody else. A plain
document that is correct beats a designed one that is nearly correct.

Tables were in that list until the agent was asked about it and answered that a
table is the one thing it would most often actually need -- "energy usage (date
| kWh | cost), bill breakdowns (item | amount), diary summaries" -- and that
forbidding them in the output loses the structure rather than the decoration.
It was right, and it named its own conditions: simple only, no nesting, no
spans. So a table here is a header row and body rows, columns measured from
their contents, cells that wrap, and no rules except one under the header. The
fragile parts of table layout are the parts that are absent.

**The margin, and why lines are conservative.** Wrapping a PDF line needs the
width of every glyph. The widths below are Adobe's published Helvetica metrics,
and a transcription error in a table of ninety-five numbers is exactly the kind
of fault that is invisible until it is in print. So the wrap allows a margin:
if a width here is wrong, the symptom is a line that stops slightly early,
never one that runs off the page. A test asserts that no rendered line exceeds
the text area under the widest plausible reading of the table.
"""

import hashlib
import os
import re
import time
import zipfile
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

# ---- the document model ----------------------------------------------------
#
# Four block kinds and nothing else. The parser below is the only thing that
# makes them, so a caller cannot invent a fifth.

HEADING = "heading"
PARAGRAPH = "paragraph"
BULLETS = "bullets"
TABLE = "table"
RULE = "rule"

MAX_COLUMNS = 10             # past this nothing fits on a page anyway
MAX_ROWS = 300
MAX_CELL = 300

FORMATS = ("pdf", "docx", "md", "txt")
DEFAULT_FORMAT = "pdf"
DEFAULT_DIR = "/var/lib/jarvis/documents"

MAX_CHARS = 200_000          # a document, not an archive
MAX_BLOCKS = 4_000
MAX_NAME = 80
MAX_TITLE = 200


class ComposeError(Exception):
    """The document could not be written, with the reason in the message."""


def parse(text: str) -> list:
    """Markdown-lite to blocks. Deterministic, and small on purpose.

    A model asked for "a document" produces markdown whatever it is told, so
    the conventions it already reaches for are the ones understood here:
    ``#`` headings, ``-`` or ``*`` bullets, ``1.`` numbered items, ``---``
    rules, blank lines between paragraphs. Anything else is a paragraph.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")[:MAX_CHARS]
    blocks: list = []
    paragraph: list = []
    bullets: list = []
    table: list = []

    def flush():
        if paragraph:
            blocks.append((PARAGRAPH, " ".join(paragraph).strip()))
            paragraph.clear()
        if bullets:
            blocks.append((BULLETS, list(bullets)))
            bullets.clear()
        if table:
            built = _table(table)
            if built:
                blocks.append((TABLE, built))
            else:
                # Not a table after all -- one row, one column, or nothing
                # under the header. The lines still have to appear: dropping
                # them because the shape was not recognised loses the
                # operator's words to a parser's opinion. Its own test caught
                # this returning an empty document for a single pipe line.
                for line in table:
                    blocks.append((PARAGRAPH, line))
            table.clear()

    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            # A markdown pipe row. Gathered whole and validated in one place,
            # because a table half-recognised is worse than one not recognised
            # at all: the rows that did not parse vanish.
            if paragraph or bullets:
                flush()
            table.append(stripped)
            continue
        if table:
            flush()
        if re.fullmatch(r"(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}", stripped):
            flush()
            blocks.append((RULE, ""))
            continue
        head = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if head:
            flush()
            blocks.append((HEADING, (len(head.group(1)), head.group(2).strip())))
            continue
        item = re.match(r"^[-*•]\s+(.*)$", stripped) or re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if item:
            if paragraph:
                blocks.append((PARAGRAPH, " ".join(paragraph).strip()))
                paragraph.clear()
            bullets.append(item.group(1).strip())
            continue
        if bullets:
            # A continuation line under a bullet belongs to that bullet.
            bullets[-1] = f"{bullets[-1]} {stripped}"
            continue
        paragraph.append(stripped)
        if len(blocks) >= MAX_BLOCKS:
            break
    flush()
    return blocks[:MAX_BLOCKS]


def _cells(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip()[:MAX_CELL] for cell in body.split("|")]


def _is_separator(line: str) -> bool:
    return all(re.fullmatch(r":?-{1,}:?", cell.replace(" ", "") or "-")
               for cell in _cells(line)) and "-" in line


def _table(lines) -> Optional[dict]:
    """Pipe rows to a header and body, or None when it is not a table.

    Every row is squared to the header's width: a ragged table is a mistake in
    the source, and quietly dropping the extra cell loses data while quietly
    keeping it puts a value under the wrong heading. Padding is the only one of
    the three that is visible in the output.
    """
    rows = [_cells(line) for line in lines if line.strip()]
    rows = [row for row, line in zip(rows, [l for l in lines if l.strip()])
            if not _is_separator(line)]
    rows = [row for row in rows if any(cell for cell in row)]
    if len(rows) < 2:
        return None
    header = rows[0][:MAX_COLUMNS]
    width = len(header)
    if width < 2:
        return None
    body = []
    for row in rows[1:MAX_ROWS + 1]:
        row = row[:width]
        body.append(row + [""] * (width - len(row)))
    if not body:
        return None
    return {"header": header, "rows": body}


def _numeric_columns(table: dict) -> set:
    """Columns whose every body cell reads as a number, for right alignment.

    Deterministic and narrow: a column of money or counts lines up on its
    digits, which is most of what makes a bill readable, and anything that is
    not unambiguously numeric is left alone.
    """
    out = set()
    for i in range(len(table["header"])):
        values = [row[i].strip() for row in table["rows"] if row[i].strip()]
        if not values:
            continue
        if all(re.fullmatch(r"[-+£$€]?\s*[\d,]+(?:\.\d+)?\s*%?", v) for v in values):
            out.add(i)
    return out


def plain(blocks, title: str = "") -> str:
    """The blocks back as plain text. Also what .txt is written from."""
    out = []
    if title:
        out += [title, "=" * min(len(title), 78), ""]
    for kind, value in blocks:
        if kind == HEADING:
            level, body = value
            out += ["", body, ("-" if level > 1 else "=") * min(len(body), 78), ""]
        elif kind == PARAGRAPH:
            out += [value, ""]
        elif kind == BULLETS:
            out += [f"  * {item}" for item in value] + [""]
        elif kind == TABLE:
            widths = [max(len(row[i]) for row in [value["header"]] + value["rows"])
                      for i in range(len(value["header"]))]
            def line(cells):
                return "  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
            out += [line(value["header"]),
                    "  " + "  ".join("-" * w for w in widths)]
            out += [line(row) for row in value["rows"]] + [""]
        elif kind == RULE:
            out += ["-" * 40, ""]
    return "\n".join(out).strip() + "\n"


def markdown(blocks, title: str = "") -> str:
    out = [f"# {title}", ""] if title else []
    for kind, value in blocks:
        if kind == HEADING:
            level, body = value
            out += [f"{'#' * min(level + 1, 6)} {body}", ""]
        elif kind == PARAGRAPH:
            out += [value, ""]
        elif kind == BULLETS:
            out += [f"- {item}" for item in value] + [""]
        elif kind == TABLE:
            out += ["| " + " | ".join(value["header"]) + " |",
                    "|" + "|".join("---" for _ in value["header"]) + "|"]
            out += ["| " + " | ".join(row) + " |" for row in value["rows"]] + [""]
        elif kind == RULE:
            out += ["---", ""]
    return "\n".join(out).strip() + "\n"


# ---- Word ------------------------------------------------------------------
#
# A .docx is a ZIP with four parts that matter. Styles are declared here rather
# than relied on: Word's built-in "Heading 1" is a latent style that may or may
# not be defined in a given document, and a heading that silently renders as
# body text is worse than no heading.

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_DOCX_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_styles() -> str:
    def style(sid, name, size_half_points, bold, space_before, outline=None):
        outline_xml = f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else ""
        return (
            f'<w:style w:type="paragraph" w:styleId="{sid}">'
            f'<w:name w:val="{name}"/><w:qFormat/>'
            f'<w:pPr><w:spacing w:before="{space_before}" w:after="120"/>{outline_xml}</w:pPr>'
            f'<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
            f'<w:sz w:val="{size_half_points}"/>'
            f'{"<w:b/>" if bold else ""}</w:rPr></w:style>')
    return ("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="%s">""" % _W
            + style("Normal", "Normal", 22, False, 0)
            + style("Title", "Title", 44, True, 0)
            + style("Heading1", "heading 1", 32, True, 320, outline=0)
            + style("Heading2", "heading 2", 26, True, 260, outline=1)
            + style("Heading3", "heading 3", 24, True, 220, outline=2)
            + style("ListBullet", "List Bullet", 22, False, 0)
            + "</w:styles>")


def _docx_core(title: str) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{_xml_escape(title)}</dc:title>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
            '</cp:coreProperties>')


def _docx_paragraph(style: str, text: str, bullet: bool = False) -> str:
    # numPr is deliberately absent: a real numbering definition is another part
    # and another thing to get wrong. A bullet glyph in the run is a bullet in
    # every reader, including the ones that are not Word.
    body = _xml_escape(text)
    prefix = "•  " if bullet else ""
    indent = '<w:ind w:left="360"/>' if bullet else ""
    return (f'<w:p><w:pPr><w:pStyle w:val="{style}"/>{indent}</w:pPr>'
            f'<w:r><w:t xml:space="preserve">{_xml_escape(prefix)}{body}</w:t></w:r></w:p>')


def _docx_table(table: dict) -> str:
    """A real Word table: a grid, a header row and body rows. Nothing else.

    No merged cells, no nesting, no column widths from the caller. Word lays
    the columns out itself from the grid, which is the part of table layout
    that it is better at than a generator guessing points.
    """
    columns = len(table["header"])
    share = int(9000 / max(1, columns))
    grid = "".join(f'<w:gridCol w:w="{share}"/>' for _ in range(columns))

    def cell(text, bold):
        run = (f'<w:r><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
               f'<w:sz w:val="20"/>{"<w:b/>" if bold else ""}</w:rPr>'
               f'<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r>')
        return (f'<w:tc><w:tcPr><w:tcW w:w="{share}" w:type="dxa"/></w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:before="20" w:after="20"/></w:pPr>'
                f'{run}</w:p></w:tc>')

    def row(cells, bold):
        head = '<w:trPr><w:tblHeader/></w:trPr>' if bold else ''
        return ("<w:tr>" + head
                + "".join(cell(text, bold) for text in cells) + "</w:tr>")

    borders = "".join(
        f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
            f'<w:tblBorders>{borders}</w:tblBorders>'
            '<w:tblLayout w:type="autofit"/></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>'
            + row(table["header"], True)
            + "".join(row(body, False) for body in table["rows"])
            + "</w:tbl>"
            # Word treats two adjacent tables as one; an empty paragraph after
            # each keeps them apart and gives the next block somewhere to sit.
            + '<w:p/>')


def to_docx(blocks, title: str = "") -> bytes:
    """A Word document. Opens in Word, Pages, LibreOffice and Google Docs."""
    import io
    parts = []
    if title:
        parts.append(_docx_paragraph("Title", title))
    for kind, value in blocks:
        if kind == HEADING:
            level, body = value
            parts.append(_docx_paragraph(f"Heading{min(max(level, 1), 3)}", body))
        elif kind == PARAGRAPH:
            parts.append(_docx_paragraph("Normal", value))
        elif kind == BULLETS:
            parts += [_docx_paragraph("ListBullet", item, bullet=True) for item in value]
        elif kind == TABLE:
            parts.append(_docx_table(value))
        elif kind == RULE:
            parts.append('<w:p><w:pPr><w:pBdr>'
                         '<w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/>'
                         '</w:pBdr></w:pPr></w:p>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{_W}"><w:body>' + "".join(parts)
                + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                  '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
                  '</w:sectPr></w:body></w:document>')
    buffer = io.BytesIO()
    # Deterministic: a fixed timestamp in the ZIP entries, so composing the
    # same document twice gives the same bytes and the same SHA-256. A hash
    # that changes because the clock moved is not a hash of the content.
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in (("[Content_Types].xml", _DOCX_CONTENT_TYPES),
                              ("_rels/.rels", _DOCX_RELS),
                              ("word/_rels/document.xml.rels", _DOCX_DOC_RELS),
                              ("word/styles.xml", _docx_styles()),
                              ("docProps/core.xml", _docx_core(title)),
                              ("word/document.xml", document)):
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, payload.encode("utf-8"))
    return buffer.getvalue()


# ---- PDF -------------------------------------------------------------------

# Adobe's published Helvetica widths, in 1/1000 em, for printable ASCII. Used
# only to decide where a line breaks; see the note at the top of this file for
# why the wrap leaves a margin rather than trusting them exactly.
_HELVETICA = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556,
    "7": 556, "8": 556, "9": 556,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
    "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
    "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
    "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
    "{": 334, "|": 260, "}": 334, "~": 584,
}
# Anything outside the table -- an em dash, a curly quote, an accented letter --
# is measured as the widest glyph in it. Over-measuring costs a short line;
# under-measuring puts text past the margin, and only one of those is visible
# in a document somebody already sent.
_WIDEST = max(_HELVETICA.values())
# Bold is wider than regular at the same size. Rather than carry a second
# table, bold text is measured with this multiplier, which is above the real
# ratio for every glyph in Helvetica-Bold.
_BOLD_FACTOR = 1.12
# And the whole wrap is held back from the true right edge by this much, so an
# error in the table shows as a short line rather than an overrun.
_WRAP_SAFETY = 0.97

PAGE_W, PAGE_H = 595.28, 841.89          # A4 in points
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 62.0, 64.0, 62.0
TEXT_W = PAGE_W - 2 * MARGIN_X


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Width of ``text`` at ``size``, over-measuring what it does not know."""
    units = sum(_HELVETICA.get(ch, _WIDEST) for ch in text or "")
    width = units * size / 1000.0
    return width * (_BOLD_FACTOR if bold else 1.0)


def wrap(text: str, size: float, width: float, bold: bool = False) -> list:
    """Break ``text`` into lines that fit ``width``. Never splits mid-word
    unless a single word cannot fit at all, in which case it is broken rather
    than allowed to run off the page."""
    limit = width * _WRAP_SAFETY

    def chop(word: str) -> list:
        """A word wider than the line, in pieces that fit.

        A URL or a long identifier has no space to break at, and the first
        version of this let one through whole whenever it was the first word
        on a line -- the exact case a long word is likely to be in. Its own
        test caught it.
        """
        pieces = []
        while len(word) > 1 and text_width(word, size, bold) > limit:
            cut = 1
            while cut < len(word) and text_width(word[:cut + 1], size, bold) <= limit:
                cut += 1
            pieces.append(word[:cut])
            word = word[cut:]
        if word:
            pieces.append(word)
        return pieces

    lines, current = [], ""
    for word in (text or "").split():
        for piece in (chop(word) if text_width(word, size, bold) > limit else [word]):
            trial = f"{current} {piece}".strip()
            if current and text_width(trial, size, bold) > limit:
                lines.append(current)
                current = piece
            else:
                current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _pdf_text(value: str) -> bytes:
    """A PDF string literal in WinAnsi, with the three characters that must
    be escaped escaped. A character the encoding cannot carry becomes '?',
    which is visible, rather than being dropped, which is not."""
    raw = (value or "").encode("cp1252", "replace")
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):       # ( ) \
            out += b"\\" + bytes([byte])
        elif byte < 32:
            out += b" "
        else:
            out += bytes([byte])
    out += b")"
    return bytes(out)


# Space between table columns, and the smallest a column may be squeezed to.
TABLE_GAP = 10.0
TABLE_MIN_COL = 42.0

# size, leading, bold, space before
_PDF_STYLE = {
    "title": (20.0, 26.0, True, 0.0),
    1: (15.0, 20.0, True, 16.0),
    2: (12.5, 17.0, True, 13.0),
    3: (11.0, 15.0, True, 11.0),
    "body": (10.5, 15.0, False, 4.0),
    "bullet": (10.5, 15.0, False, 2.0),
    "cell": (9.5, 13.0, False, 0.0),
}


def column_widths(table: dict, size: float, available: float) -> list:
    """How wide each column gets, measured from what is in it.

    Natural widths where they fit; otherwise squeezed in proportion with a
    floor, and any leftover taken off the widest column so the total is exactly
    the space available rather than nearly it. Cells wrap inside their column
    afterwards, so a squeezed column is taller, never wider.
    """
    columns = len(table["header"])
    gaps = TABLE_GAP * (columns - 1)
    room = max(TABLE_MIN_COL * columns, available - gaps)
    natural = []
    for i in range(columns):
        body = max((text_width(row[i], size) for row in table["rows"]), default=0.0)
        head = text_width(table["header"][i], size, True)   # the header is bold
        natural.append(max(TABLE_MIN_COL, max(body, head) + 4.0))
    if sum(natural) <= room:
        return natural
    fixed = sum(w for w in natural if w <= TABLE_MIN_COL)
    flexible = [w for w in natural if w > TABLE_MIN_COL]
    spare = room - fixed
    scale = (spare / sum(flexible)) if flexible and spare > 0 else 0.0
    out = [w if w <= TABLE_MIN_COL else max(TABLE_MIN_COL, w * scale)
           for w in natural]
    drift = room - sum(out)
    if drift < 0:
        widest_at = out.index(max(out))
        out[widest_at] = max(TABLE_MIN_COL, out[widest_at] + drift)
    return out


def _layout(blocks, title: str) -> list:
    """Blocks to pages of (x, y, size, bold, text). No drawing yet.

    Laid out before anything is written so a page break is a decision made
    once, with the whole page in view, rather than discovered halfway down.
    """
    pages, page = [], []
    y = PAGE_H - MARGIN_TOP

    def newpage():
        nonlocal page, y
        if page:
            pages.append(page)
        page, y = [], PAGE_H - MARGIN_TOP

    def put(text, size, leading, bold, before, indent=0.0, width=None):
        nonlocal y
        gap = width if width is not None else TEXT_W - indent
        y -= before
        for line in wrap(text, size, gap, bold):
            if y - leading < MARGIN_BOTTOM:
                newpage()
            y -= leading
            page.append((MARGIN_X + indent, y, size, bold, line))

    if title:
        size, leading, bold, before = _PDF_STYLE["title"]
        put(title, size, leading, bold, before)
        y -= 8.0

    for kind, value in blocks:
        if kind == HEADING:
            level, body = value
            size, leading, bold, before = _PDF_STYLE[min(max(level, 1), 3)]
            put(body, size, leading, bold, before)
        elif kind == PARAGRAPH:
            size, leading, bold, before = _PDF_STYLE["body"]
            put(value, size, leading, bold, before)
        elif kind == BULLETS:
            size, leading, bold, before = _PDF_STYLE["bullet"]
            for item in value:
                # The glyph sits in the margin; the text is indented so
                # continuation lines line up under the first, not under the
                # bullet.
                y -= before
                lines = wrap(item, size, TEXT_W - 16.0, bold)
                for i, line in enumerate(lines):
                    if y - leading < MARGIN_BOTTOM:
                        newpage()
                    y -= leading
                    if i == 0:
                        page.append((MARGIN_X, y, size, False, "•"))
                    page.append((MARGIN_X + 16.0, y, size, bold, line))
        elif kind == TABLE:
            size, leading, _, _ = _PDF_STYLE["cell"]
            widths = column_widths(value, size, TEXT_W)
            right = _numeric_columns(value)
            xs, at = [], MARGIN_X
            for w in widths:
                xs.append(at)
                at += w + TABLE_GAP
            span = sum(widths) + TABLE_GAP * (len(widths) - 1)

            def put_row(cells, bold):
                """One row, as tall as its tallest cell.

                The whole row moves to the next page rather than splitting, so
                a wrapped cell never leaves half its text behind, and the
                header is repeated at the top of the new page -- a column of
                figures with no heading above it is a column of figures nobody
                can read.
                """
                nonlocal y
                wrapped = [wrap(cell, size, widths[i], bold)
                           for i, cell in enumerate(cells)]
                height = max(len(lines) for lines in wrapped) * leading
                if y - height < MARGIN_BOTTOM:
                    newpage()
                    header_row()
                top = y
                for i, lines in enumerate(wrapped):
                    for n, line in enumerate(lines):
                        # A numeric column is set flush right, which is most of
                        # what makes a column of money readable.
                        offset = (widths[i] - text_width(line, size, bold)
                                  if i in right else 0.0)
                        page.append((xs[i] + max(0.0, offset),
                                     top - (n + 1) * leading, size, bold, line))
                y = top - height

            def header_row():
                nonlocal y
                put_row(value["header"], True)
                dashes = max(1, int(span / max(1.0, text_width("—", size))))
                y -= 3.0
                page.append((MARGIN_X, y, size, False, "—" * dashes))
                y -= 5.0

            y -= 8.0
            header_row()
            for row in value["rows"]:
                put_row(row, False)
            y -= 8.0
        elif kind == RULE:
            y -= 10.0
            if y - 10.0 < MARGIN_BOTTOM:
                newpage()
            page.append((MARGIN_X, y, 10.5, False, "—" * 24))
            y -= 6.0
    newpage()
    return pages or [[]]


def to_pdf(blocks, title: str = "") -> bytes:
    """A PDF. Base-14 fonts, so nothing is embedded and nothing is licensed."""
    pages = _layout(blocks, title)
    objects: list = []          # 1-indexed on output

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                       b"/Encoding /WinAnsiEncoding >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                    b"/Encoding /WinAnsiEncoding >>")
    resources = add(b"<< /Font << /F1 %d 0 R /F2 %d 0 R >> >>"
                    % (font_regular, font_bold))

    pages_id = len(objects) + 1          # reserved; filled in below
    add(b"")                             # placeholder for the page tree

    kids = []
    for page in pages:
        stream = bytearray(b"BT\n")
        last = None
        for x, y, size, bold, text in page:
            key = ("F2" if bold else "F1", size)
            if key != last:
                stream += b"/%s %.2f Tf\n" % (key[0].encode(), size)
                last = key
            stream += b"1 0 0 1 %.2f %.2f Tm\n" % (x, y)
            stream += _pdf_text(text) + b" Tj\n"
        stream += b"ET"
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream"
                      % (len(stream), bytes(stream)))
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
                        b"/Resources %d 0 R /Contents %d 0 R >>"
                        % (pages_id, PAGE_W, PAGE_H, resources, content)))

    objects[pages_id - 1] = (b"<< /Type /Pages /Count %d /Kids [%s] >>"
                             % (len(kids), b" ".join(b"%d 0 R" % k for k in kids)))
    info = add(b"<< /Title " + _pdf_text(title or "Document")
               + b" /Producer (jarvis/compose) >>")
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, info, start))
    return bytes(out)


# ---- writing it down -------------------------------------------------------

def safe_name(name: str, fallback: str = "document") -> str:
    """A filename from a title. Nothing that can leave the directory."""
    name = os.path.basename(str(name or "").strip())
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip(" .-_")
    name = re.sub(r"\s+", "-", name).strip("-")[:MAX_NAME]
    return name or fallback


def render(blocks, title: str, fmt: str) -> bytes:
    if fmt == "docx":
        return to_docx(blocks, title)
    if fmt == "pdf":
        return to_pdf(blocks, title)
    if fmt == "md":
        return markdown(blocks, title).encode("utf-8")
    if fmt == "txt":
        return plain(blocks, title).encode("utf-8")
    raise ComposeError(f"{fmt!r} is not a format this can write "
                       f"({', '.join(FORMATS)})")


def compose(text: str, title: str = "", fmt: str = DEFAULT_FORMAT,
            directory: str = DEFAULT_DIR, name: str = "",
            max_bytes: int = 8 << 20, max_files: int = 500) -> dict:
    """Write one document and report what was written.

    Returns the path, size and SHA-256. The hash is not decoration: a document
    the agent produced and the operator sent on is a thing somebody may later
    ask about, and "this is the file, and here is its hash on the day it was
    made" is a different claim from "I wrote something like this".

    Nothing here can write outside ``directory``: the name is stripped to a
    basename and scrubbed, the extension comes from the format and not from
    the caller, and the final path is checked against the directory again
    after resolution, because a scrub is a claim and a realpath is a fact.
    """
    fmt = str(fmt or DEFAULT_FORMAT).strip().lower().lstrip(".")
    if fmt not in FORMATS:
        raise ComposeError(f"{fmt!r} is not a format this can write "
                           f"({', '.join(FORMATS)})")
    title = " ".join(str(title or "").split())[:MAX_TITLE]
    blocks = parse(text)
    if not blocks and not title:
        raise ComposeError("nothing to write")

    payload = render(blocks, title, fmt)
    if len(payload) > max_bytes:
        raise ComposeError(f"the document is {len(payload)} bytes, over the "
                           f"{max_bytes}-byte limit")

    try:
        os.makedirs(directory, mode=0o750, exist_ok=True)
    except OSError as exc:
        raise ComposeError(f"cannot use {directory}: {exc}") from exc
    existing = [f for f in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, f))]
    if len(existing) >= max_files:
        # Refused rather than tidied. Deleting the operator's documents to make
        # room for one more is not this function's decision to take.
        raise ComposeError(f"{directory} already holds {len(existing)} files "
                           f"(limit {max_files}); nothing was written")

    stem = safe_name(name or title or "document")
    root = os.path.realpath(directory)
    candidate = f"{stem}.{fmt}"
    n = 2
    while os.path.exists(os.path.join(root, candidate)):
        candidate = f"{stem}-{n}.{fmt}"
        n += 1
        if n > 999:
            raise ComposeError(f"too many documents named {stem}")
    path = os.path.realpath(os.path.join(root, candidate))
    if os.path.dirname(path) != root:
        raise ComposeError("refusing to write outside the document directory")

    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    return {"path": path, "name": os.path.basename(path), "format": fmt,
            "title": title, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "blocks": len(blocks), "written_at": time.time()}


def listing(directory: str = DEFAULT_DIR, limit: int = 100) -> list:
    """What the agent has written, newest first."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(directory, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        out.append({"name": name, "bytes": stat.st_size, "mtime": stat.st_mtime,
                    "format": name.rsplit(".", 1)[-1].lower() if "." in name else ""})
    out.sort(key=lambda row: -row["mtime"])
    return out[:max(1, int(limit))]
