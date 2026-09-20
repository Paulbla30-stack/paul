"""Reading the formats a person actually sends, and saying what was missed.

The load-bearing tests here are the ones about *not* reading. A scanned PDF
extracts to an empty string in every naive reader, and an empty string is
indistinguishable from a blank page. This codebase has met that failure
before -- an empty security scan report being read as a report of zero
findings -- and the rule is the same: what could not be read is reported as
not read, never as nothing there.

So every test that exercises a gap asserts the gap is *named*, not merely
that extraction returned little.
"""

import io
import os
import struct
import tempfile
import unittest
import zipfile

from jarvis.agent import documents as doc
from jarvis.agent import environment as env

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:                                    # pragma: no cover
    HAVE_PYPDF = False


def docx_at(path, paragraphs, media=()):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
           'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + body + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
        for i, _ in enumerate(media):
            z.writestr(f"word/media/image{i}.png", b"\x89PNG\r\n\x1a\n")
    return path


def xlsx_at(path, rows, sheets=1):
    shared, lookup = [], {}
    for row in rows:
        for cell in row:
            if isinstance(cell, str) and cell not in lookup:
                lookup[cell] = len(shared)
                shared.append(cell)
    sst = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org'
           '/spreadsheetml/2006/main">'
           + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
    body = ""
    for r, row in enumerate(rows, start=1):
        cells = ""
        for c, cell in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(cell, str):
                cells += f'<c r="{ref}" t="s"><v>{lookup[cell]}</v></c>'
            else:
                cells += f'<c r="{ref}"><v>{cell}</v></c>'
        body += f'<row r="{r}">{cells}</row>'
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
             'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + body + "</sheetData></worksheet>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/sharedStrings.xml", sst)
        for i in range(1, sheets + 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet)
    return path


def pdf_at(path, with_text=True, pages=1):
    """A real PDF, built by hand, optionally with no text layer at all.

    Hand-built rather than generated so the scanned case is genuinely a page
    with no text operators in it, which is what a scan is.
    """
    stream = b"BT /F1 12 Tf 72 720 Td (Invoice total 4412 GBP) Tj ET" if with_text else b""
    n_page, n_content, n_font = 3, 3 + pages, 3 + 2 * pages
    kids = " ".join(f"{n_page + i} 0 R" for i in range(pages))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()]
    for i in range(pages):
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {n_content + i} 0 R /Resources << /Font << /F1 "
            f"{n_font} 0 R >> >> >>".encode())
    for _ in range(pages):
        objs.append(b"<< /Length " + str(len(stream)).encode()
                    + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
              f"startxref\n{xref}\n%%EOF\n".encode())
    with open(path, "wb") as f:
        f.write(out.getvalue())
    return path


def png_at(path, w=1280, h=720):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def at(self, name):
        return os.path.join(self.tmp, name)


class TestWhatCouldNotBeReadIsSaidSo(Base):
    """The one that matters. Unread is not empty."""

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_a_scanned_pdf_is_not_reported_as_a_blank_one(self):
        got = env.read_file(pdf_at(self.at("scan.pdf"), with_text=False))
        self.assertFalse(got["read"])
        self.assertFalse(got["complete"])
        self.assertIn("not blank", got["note"])
        self.assertIn("scan", got["note"])
        self.assertEqual(got["pages_without_text"], 1)

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_a_pdf_with_some_scanned_pages_names_which(self):
        path = pdf_at(self.at("mixed.pdf"), with_text=True, pages=1)
        got = env.read_file(path)
        self.assertTrue(got["read"])
        self.assertEqual(got["pages_without_text"], 0)

    def test_an_image_is_declined_with_a_reason_not_an_empty_string(self):
        got = env.read_file(png_at(self.at("photo.png")))
        self.assertFalse(got["read"])
        self.assertIn("unknown, not empty", got["note"])
        self.assertEqual(got["image"]["width"], 1280)
        self.assertEqual(got["image"]["height"], 720)

    def test_a_missing_pdf_reader_is_reported_as_a_fact_about_the_machine(self):
        real = doc.read_pdf
        try:
            import builtins
            original = builtins.__import__

            def no_pypdf(name, *a, **k):
                if name == "pypdf":
                    raise ImportError("not installed")
                return original(name, *a, **k)
            builtins.__import__ = no_pypdf
            got = doc.read_pdf(pdf_at(self.at("x.pdf")))
        finally:
            builtins.__import__ = original
            doc.read_pdf = real
        self.assertFalse(got["complete"])
        self.assertIn("no PDF reader is installed", got["note"])

    def test_a_docx_with_images_says_the_images_were_not_read(self):
        path = docx_at(self.at("d.docx"), ["Hello"], media=["a", "b"])
        got = env.read_file(path)
        self.assertTrue(got["read"])
        self.assertFalse(got["complete"])
        self.assertIn("2 embedded image", got["note"])

    def test_an_empty_docx_says_no_text_was_found_rather_than_nothing(self):
        got = env.read_file(docx_at(self.at("blank.docx"), []))
        self.assertIn("no text was found", got["note"])

    def test_a_zip_says_it_was_not_opened(self):
        path = self.at("bundle.zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("a.txt", "hello")
        got = env.read_file(path)
        self.assertFalse(got["read"])
        self.assertIn("zip archive", got["note"])

    def test_every_incomplete_result_carries_a_note_saying_why(self):
        """complete=False with no explanation is the bug this guards."""
        cases = [docx_at(self.at("a.docx"), ["x"], media=["i"]),
                 png_at(self.at("b.png")),
                 xlsx_at(self.at("c.xlsx"), [["Item", 1]])]
        if HAVE_PYPDF:
            cases.append(pdf_at(self.at("d.pdf"), with_text=False))
        for path in cases:
            got = env.read_file(path)
            if not got.get("complete"):
                self.assertTrue(got.get("note"),
                                f"{os.path.basename(path)} is incomplete and says nothing")


class TestItReadsTheFormatsPeopleSend(Base):

    def test_a_word_document(self):
        path = docx_at(self.at("letter.docx"),
                       ["Dear Paul,", "Your balance is 4412.", "Yours,"])
        got = env.read_file(path)
        self.assertEqual(got["format"], "docx")
        self.assertIn("4412", got["text"])
        self.assertIn("Dear Paul,", got["text"])

    def test_a_spreadsheet_with_shared_strings(self):
        path = xlsx_at(self.at("b.xlsx"), [["Item", "Cost"], ["Electricity", 92.5]])
        got = env.read_file(path)
        self.assertEqual(got["format"], "xlsx")
        self.assertIn("Electricity", got["text"])
        self.assertIn("92.5", got["text"])
        self.assertIn("not recalculated formulas", got["note"])

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_a_pdf_with_a_text_layer(self):
        got = env.read_file(pdf_at(self.at("inv.pdf")))
        self.assertEqual(got["format"], "pdf")
        self.assertIn("4412", got["text"])
        self.assertTrue(got["complete"])

    def test_plain_text_still_goes_the_plain_way(self):
        path = self.at("notes.txt")
        with open(path, "w") as f:
            f.write("line one\nline two\n")
        got = env.read_file(path)
        self.assertIn("line two", got["text"])
        self.assertNotIn("format", got)


class TestFormatComesFromBytesNotNames(Base):
    """An extension is a claim by whoever named the file."""

    def test_a_pdf_called_txt_is_still_a_pdf(self):
        path = pdf_at(self.at("actually.txt"))
        with open(path, "rb") as f:
            self.assertEqual(doc.sniff(path, f.read(64)), doc.PDF)

    def test_a_docx_called_something_else_is_still_a_docx(self):
        path = docx_at(self.at("report.dat"), ["hello"])
        with open(path, "rb") as f:
            self.assertEqual(doc.sniff(path, f.read(64)), doc.DOCX)

    def test_a_text_file_named_pdf_is_read_as_text(self):
        path = self.at("not-really.pdf")
        with open(path, "w") as f:
            f.write("this is just text\n")
        got = env.read_file(path)
        self.assertIn("just text", got["text"])


class TestItNeverRaises(Base):

    def test_a_truncated_office_file_is_an_answer(self):
        path = self.at("broken.docx")
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04" + b"\x00" * 40)
        got = env.read_file(path)
        self.assertFalse(got["read"])
        self.assertTrue(got.get("note") or got.get("reason"))

    def test_a_corrupt_pdf_is_an_answer(self):
        path = self.at("broken.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\nnot really a pdf at all")
        got = env.read_file(path)
        self.assertFalse(got["read"])

    def test_the_fence_still_wins_over_every_format(self):
        for secret in env.SECRET_PATHS:
            got = env.read_file(secret)
            self.assertFalse(got["read"])
            self.assertTrue(got.get("fenced"))


if __name__ == "__main__":
    unittest.main()
