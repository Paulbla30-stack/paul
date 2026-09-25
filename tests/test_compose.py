"""Writing documents a person can open.

The agent could read a statement and could not produce a letter. "I have
written it out below", followed by four hundred words in a chat window, is not
a document: it cannot be printed, attached, filed or signed.

These tests are mostly about the two things that are invisible until the file
is already with somebody else — text running off the right-hand edge of a page,
and a path escaping the directory it was supposed to be written in.
"""

import hashlib
import os
import tempfile
import unittest
import zipfile

from jarvis.agent import compose

NL = chr(10)


SAMPLE = """# Heading one

A paragraph that is long enough to need wrapping, which is the point of it,
because a wrap that is never exercised is a wrap that has never been checked
and the failure it hides only appears once the document is with somebody else.

## Heading two

- first bullet
- second bullet, with enough text on it to run past the end of one line and
  carry on to a second, which is where a bullet's indentation goes wrong
- third

---

1. numbered items become bullets, because a number is a claim about order
2. and the order is already in the list
"""


class TestTheParser(unittest.TestCase):

    def test_it_finds_the_four_kinds_and_no_others(self):
        kinds = {kind for kind, _ in compose.parse(SAMPLE)}
        self.assertEqual(kinds, {compose.HEADING, compose.PARAGRAPH,
                                 compose.BULLETS, compose.RULE})

    def test_heading_levels_are_kept(self):
        levels = [value[0] for kind, value in compose.parse(SAMPLE)
                  if kind == compose.HEADING]
        self.assertEqual(levels, [1, 2])

    def test_a_continuation_line_belongs_to_its_bullet(self):
        blocks = dict((k, v) for k, v in compose.parse(
            "- one line\n  and its continuation\n- two\n"))
        self.assertEqual(blocks[compose.BULLETS],
                         ["one line and its continuation", "two"])

    def test_numbered_items_are_list_items(self):
        blocks = compose.parse("1. first\n2. second\n")
        self.assertEqual(blocks, [(compose.BULLETS, ["first", "second"])])

    def test_plain_prose_is_one_paragraph_per_blank_line(self):
        blocks = compose.parse("one\nstill one\n\ntwo\n")
        self.assertEqual(blocks, [(compose.PARAGRAPH, "one still one"),
                                  (compose.PARAGRAPH, "two")])

    def test_nothing_in_is_nothing_out(self):
        for empty in ("", "   \n\n  ", None):
            self.assertEqual(compose.parse(empty), [])


class TestNothingRunsOffThePage(unittest.TestCase):
    """The one failure that is invisible until it is in print.

    Every width here is measured with the table over-reading, so a line that
    passes these assertions passes them under the worst reading of the table
    rather than the most flattering one.
    """

    def widest_possible(self, text, size, bold):
        # Measure as though every glyph were the widest in the font: if a line
        # fits under that, no transcription error in the width table can push
        # it off the page.
        return len(text) * compose._WIDEST * size / 1000.0 * (
            compose._BOLD_FACTOR if bold else 1.0)

    def test_every_wrapped_line_fits_the_text_area(self):
        for size in (10.5, 15.0, 20.0):
            for bold in (False, True):
                lines = compose.wrap(SAMPLE.replace("\n", " "), size,
                                     compose.TEXT_W, bold)
                for line in lines:
                    self.assertLessEqual(
                        compose.text_width(line, size, bold), compose.TEXT_W,
                        f"{size} bold={bold}: {line!r}")

    def test_a_word_longer_than_the_page_is_broken_not_overflowed(self):
        monster = "A" * 400
        for line in compose.wrap(monster, 10.5, compose.TEXT_W):
            self.assertLessEqual(compose.text_width(line, 10.5), compose.TEXT_W)

    def test_characters_the_table_does_not_know_are_over_measured(self):
        # An em dash, a curly quote and an accented letter are not in the
        # ASCII table. Guessing them narrow puts text past the margin.
        for ch in ("—", "“", "é", "•"):
            self.assertEqual(compose.text_width(ch, 10.0),
                             compose._WIDEST * 10.0 / 1000.0)

    def test_the_widths_cover_every_printable_ascii_character(self):
        missing = [chr(c) for c in range(32, 127) if chr(c) not in compose._HELVETICA]
        self.assertEqual(missing, [])

    def test_the_widths_that_are_easy_to_get_wrong_are_right(self):
        # Spot values from Adobe's Helvetica metrics.
        for ch, width in ((" ", 278), ("i", 222), ("l", 222), ("M", 833),
                          ("W", 944), ("0", 556), ("9", 556), ("@", 1015)):
            self.assertEqual(compose._HELVETICA[ch], width, ch)

    def test_nothing_is_laid_out_below_the_bottom_margin(self):
        pages = compose._layout(compose.parse(SAMPLE * 12), "A long one")
        self.assertGreater(len(pages), 1, "the sample should need several pages")
        for page in pages:
            for x, y, size, bold, text in page:
                self.assertGreaterEqual(y, compose.MARGIN_BOTTOM - size)
                self.assertGreaterEqual(x, compose.MARGIN_X)
                self.assertLessEqual(
                    x + compose.text_width(text, size, bold),
                    compose.PAGE_W - compose.MARGIN_X + 0.5, repr(text))


TABLE_MD = """| Item | Units | Amount |
|---|---|---|
| Electricity, on the standard tariff, with a description long enough to wrap | 412 | 84.20 |
| Gas | 88 | 41.05 |
| Standing charge | | 9.60 |
"""


class TestTables(unittest.TestCase):
    """The one thing the agent said it would most often actually need.

    Asked whether leaving tables out was right, it agreed about images and
    disagreed about tables, with examples: energy usage, bill breakdowns,
    diary summaries. It named its own conditions — simple only, no nesting,
    no spans — and they are what is built.
    """

    def table(self, md=TABLE_MD):
        blocks = compose.parse(md)
        found = [value for kind, value in blocks if kind == compose.TABLE]
        self.assertEqual(len(found), 1, blocks)
        return found[0]

    def test_a_pipe_table_is_recognised(self):
        got = self.table()
        self.assertEqual(got["header"], ["Item", "Units", "Amount"])
        self.assertEqual(len(got["rows"]), 3)

    def test_the_separator_row_is_not_data(self):
        self.assertNotIn(["---", "---", "---"], self.table()["rows"])
        for row in self.table()["rows"]:
            self.assertNotEqual(set("".join(row)), {"-"})

    def test_text_either_side_stays_separate(self):
        blocks = compose.parse("Before it." + NL + NL + TABLE_MD + NL + "After it." + NL)
        kinds = [kind for kind, _ in blocks]
        self.assertEqual(kinds, [compose.PARAGRAPH, compose.TABLE,
                                 compose.PARAGRAPH])

    def test_a_ragged_row_is_padded_not_shifted(self):
        # Dropping the extra cell loses data; keeping it puts a value under the
        # wrong heading. Padding is the only one of the three that is visible.
        got = self.table("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n")
        self.assertEqual(got["rows"], [["1", ""], ["1", "2"]])

    def test_one_row_is_not_a_table(self):
        self.assertEqual([k for k, _ in compose.parse("| just | one |\n")],
                         [compose.PARAGRAPH])

    def test_a_single_column_is_not_a_table(self):
        self.assertNotIn(compose.TABLE,
                         [k for k, _ in compose.parse("| one |\n|---|\n| two |\n")])

    def test_numeric_columns_are_found_and_prose_is_not(self):
        numeric = compose._numeric_columns(self.table())
        self.assertIn(2, numeric, "the money column")
        self.assertNotIn(0, numeric, "the description column")

    def test_money_and_percentages_count_as_numeric(self):
        got = self.table("| a | b |\n|---|---|\n| £4.50 | 12% |\n| £9 | 7% |\n")
        self.assertEqual(compose._numeric_columns(got), {0, 1})

    def test_the_columns_fit_the_page(self):
        for md in (TABLE_MD,
                   "| " + " | ".join("c" * 30 for _ in range(6)) + " |\n"
                   + "|" + "|".join("---" for _ in range(6)) + "|\n"
                   + "| " + " | ".join("x" * 40 for _ in range(6)) + " |\n"):
            table = self.table(md)
            widths = compose.column_widths(table, 9.5, compose.TEXT_W)
            total = sum(widths) + compose.TABLE_GAP * (len(widths) - 1)
            self.assertLessEqual(round(total, 3), round(compose.TEXT_W, 3), md[:40])
            for w in widths:
                self.assertGreaterEqual(w, compose.TABLE_MIN_COL)

    def test_no_cell_is_laid_out_past_the_margin(self):
        pages = compose._layout(compose.parse(TABLE_MD * 6), "A bill")
        for page in pages:
            for x, y, size, bold, text in page:
                self.assertLessEqual(
                    x + compose.text_width(text, size, bold),
                    compose.PAGE_W - compose.MARGIN_X + 0.5, repr(text))

    def test_the_header_is_repeated_on_a_new_page(self):
        # A column of figures with no heading above it is a column of figures
        # nobody can read.
        pages = compose._layout(compose.parse(TABLE_MD.split("\n")[0] + "\n"
                                              + TABLE_MD.split("\n")[1] + "\n"
                                              + "| Gas | 88 | 41.05 |\n" * 120),
                                "A long bill")
        self.assertGreater(len(pages), 1)
        for n, page in enumerate(pages):
            texts = [text for _, _, _, _, text in page]
            self.assertIn("Amount", texts, f"page {n + 1} has no header")

    def test_the_word_file_gets_a_real_table(self):
        import io
        import xml.etree.ElementTree as ET
        raw = compose.to_docx(compose.parse(TABLE_MD), "A bill")
        zf = zipfile.ZipFile(io.BytesIO(raw))
        body = zf.read("word/document.xml").decode()
        ET.fromstring(body)
        self.assertIn("<w:tbl>", body)
        self.assertEqual(body.count("<w:tc>"), 3 * 4)     # header + three rows
        self.assertIn("<w:tblHeader/>", body)             # repeats across pages

    def test_a_cell_cannot_inject_xml(self):
        import io
        import xml.etree.ElementTree as ET
        raw = compose.to_docx(
            compose.parse('| a | b |\n|---|---|\n| </w:t></w:r><w:r><w:t>x | & < > |\n'),
            "Escaping")
        zf = zipfile.ZipFile(io.BytesIO(raw))
        ET.fromstring(zf.read("word/document.xml"))

    def test_plain_text_and_markdown_keep_the_columns(self):
        blocks = compose.parse(TABLE_MD)
        self.assertIn("Amount", compose.plain(blocks))
        self.assertIn("| Amount |", compose.markdown(blocks))

    def test_the_pdf_reads_back_with_its_figures(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed")
        import io
        reader = PdfReader(io.BytesIO(compose.to_pdf(compose.parse(TABLE_MD), "A bill")))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        for value in ("Amount", "84.20", "41.05", "9.60", "Standing charge"):
            self.assertIn(value, text)


class TestEveryGlyphItDrawsCanBeEncoded(unittest.TestCase):
    """A rule drawn with a character WinAnsi does not have is a row of '?'.

    U+2500 was used for the horizontal rule and the table underline, and it is
    not in WinAnsiEncoding, so every rule in every PDF would have rendered as
    question marks. Caught by reading the output rather than the code.
    """

    def drawn(self, md):
        pages = compose._layout(compose.parse(md), "A title")
        return "".join(text for page in pages for *_, text in page)

    def test_rules_and_bullets_survive_the_encoding(self):
        for md in ("a\n\n---\n\nb\n", TABLE_MD, "- one\n- two\n"):
            drawn = self.drawn(md)
            self.assertNotIn("?", compose._pdf_text(drawn).decode("latin-1"),
                             f"a glyph became '?' in: {md[:30]!r}")


class TestThePDF(unittest.TestCase):

    def pdf(self, text=SAMPLE, title="A document"):
        return compose.to_pdf(compose.parse(text), title)

    def test_it_is_a_pdf(self):
        self.assertTrue(self.pdf().startswith(b"%PDF-1.4"))
        self.assertTrue(self.pdf().rstrip().endswith(b"%%EOF"))

    def test_the_cross_reference_offsets_point_at_their_objects(self):
        # A PDF with a wrong xref opens in some readers and not others, which
        # is the worst way for this to fail: it works on the machine that made
        # it and not on the one it was sent to.
        raw = self.pdf()
        start = int(raw[raw.rindex(b"startxref") + 9:raw.rindex(b"%%EOF")].strip())
        table = raw[start:]
        self.assertTrue(table.startswith(b"xref"))
        count = int(table.split(b"\n")[1].split()[1])
        for number in range(1, count):
            line = table.split(b"\n")[2 + number]
            offset = int(line.split()[0])
            self.assertEqual(raw[offset:offset + len(b"%d 0 obj" % number)],
                             b"%d 0 obj" % number, f"object {number}")

    def test_a_reader_can_get_the_words_back_out(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed")
        import io
        reader = PdfReader(io.BytesIO(self.pdf()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        for phrase in ("A document", "Heading one", "Heading two", "first bullet"):
            self.assertIn(phrase, text)

    def test_brackets_and_backslashes_do_not_break_the_syntax(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf not installed")
        import io
        raw = self.pdf("A line with (parentheses), a \\backslash and a ) stray.",
                       "Escaping (test)")
        reader = PdfReader(io.BytesIO(raw))
        self.assertIn("backslash", reader.pages[0].extract_text() or "")

    def test_a_character_the_encoding_cannot_carry_is_visible_not_dropped(self):
        # '?' is a bad rendering. A silently missing word is a wrong document.
        self.assertIn(b"?", compose._pdf_text("中文"))


class TestTheWordFile(unittest.TestCase):

    def docx(self, text=SAMPLE, title="A document"):
        return compose.to_docx(compose.parse(text), title)

    def test_it_is_a_valid_zip_with_the_parts_word_needs(self):
        import io
        zf = zipfile.ZipFile(io.BytesIO(self.docx()))
        self.assertIsNone(zf.testzip())
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml",
                     "word/styles.xml", "word/_rels/document.xml.rels"):
            self.assertIn(part, zf.namelist())

    def test_every_part_is_well_formed_xml(self):
        import io
        import xml.etree.ElementTree as ET
        zf = zipfile.ZipFile(io.BytesIO(self.docx("Text with <angle> & ampersand")))
        for name in zf.namelist():
            ET.fromstring(zf.read(name))

    def test_the_heading_styles_it_uses_are_the_ones_it_defines(self):
        # Word's built-in headings are latent styles that may not be defined.
        # A heading that silently renders as body text is worse than none.
        import io
        import re
        zf = zipfile.ZipFile(io.BytesIO(self.docx()))
        document = zf.read("word/document.xml").decode()
        styles = zf.read("word/styles.xml").decode()
        used = set(re.findall(r'w:pStyle w:val="([^"]+)"', document))
        defined = set(re.findall(r'w:styleId="([^"]+)"', styles))
        self.assertTrue(used, "no styles used at all")
        self.assertTrue(used <= defined, f"undefined: {sorted(used - defined)}")

    def test_the_text_survives(self):
        import io
        zf = zipfile.ZipFile(io.BytesIO(self.docx()))
        body = zf.read("word/document.xml").decode()
        for phrase in ("A document", "Heading one", "first bullet"):
            self.assertIn(phrase, body)

    def test_the_same_document_twice_is_the_same_bytes(self):
        # The hash is reported to the operator, so it has to be a hash of the
        # content and not of the clock.
        self.assertEqual(hashlib.sha256(self.docx()).hexdigest(),
                         hashlib.sha256(self.docx()).hexdigest())


class TestWhereItIsAllowedToWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "documents")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **kw):
        kw.setdefault("text", SAMPLE)
        kw.setdefault("title", "A document")
        kw.setdefault("directory", self.dir)
        return compose.compose(**kw)

    def test_every_format_lands_on_disk_with_its_hash(self):
        for fmt in compose.FORMATS:
            got = self.write(fmt=fmt, name=f"doc-{fmt}")
            self.assertTrue(os.path.isfile(got["path"]), fmt)
            with open(got["path"], "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                                 got["sha256"], fmt)
            self.assertEqual(got["bytes"], os.path.getsize(got["path"]), fmt)

    def test_a_name_cannot_climb_out_of_the_directory(self):
        for attempt in ("../escape", "../../etc/passwd", "/etc/passwd",
                        "..\\escape", "a/b/c", "....//escape"):
            got = self.write(name=attempt, fmt="txt")
            self.assertEqual(os.path.dirname(got["path"]),
                             os.path.realpath(self.dir), attempt)

    def test_the_extension_comes_from_the_format_not_the_caller(self):
        got = self.write(name="report.docx", fmt="txt")
        self.assertTrue(got["path"].endswith(".txt"))

    def test_an_unknown_format_is_refused_before_anything_is_written(self):
        for fmt in ("exe", "sh", "html", "pdf.exe", "docx.sh", "p df"):
            with self.assertRaises(compose.ComposeError):
                self.write(fmt=fmt)
        self.assertFalse(os.path.isdir(self.dir) and os.listdir(self.dir))

    def test_an_omitted_format_is_the_default_not_an_error(self):
        # "" and None mean "you choose", which is what the signature's default
        # is for. Only a format that was actually named and is not writable is
        # a refusal.
        for fmt in ("", None):
            got = self.write(fmt=fmt, name=f"default-{fmt!r}")
            self.assertEqual(got["format"], compose.DEFAULT_FORMAT)

    def test_case_and_a_leading_dot_do_not_decide_the_format(self):
        for spelling in ("PDF", ".pdf", " Pdf "):
            self.assertEqual(self.write(fmt=spelling, name=f"c{len(spelling)}")["format"],
                             "pdf")

    def test_two_documents_with_one_name_do_not_overwrite_each_other(self):
        first = self.write(name="report", fmt="txt")
        second = self.write(name="report", fmt="txt")
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(os.path.isfile(first["path"]))

    def test_a_full_directory_is_refused_rather_than_tidied(self):
        # Deleting the operator's documents to make room is not this
        # function's decision to take.
        os.makedirs(self.dir)
        for i in range(5):
            open(os.path.join(self.dir, f"old-{i}.txt"), "w").close()
        with self.assertRaises(compose.ComposeError) as caught:
            self.write(fmt="txt", max_files=5)
        self.assertIn("nothing was written", str(caught.exception))
        self.assertEqual(len(os.listdir(self.dir)), 5)

    def test_an_oversized_document_is_refused(self):
        with self.assertRaises(compose.ComposeError):
            self.write(text="word " * 50_000, fmt="txt", max_bytes=1000)

    def test_nothing_to_write_is_an_error_not_an_empty_file(self):
        with self.assertRaises(compose.ComposeError):
            self.write(text="", title="", fmt="txt")

    def test_no_part_file_is_left_behind(self):
        self.write(fmt="pdf")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".part")], [])

    def test_the_listing_is_newest_first(self):
        import time
        names = []
        for i in range(3):
            names.append(self.write(name=f"doc-{i}", fmt="txt")["name"])
            time.sleep(0.01)
        self.assertEqual([row["name"] for row in compose.listing(self.dir)],
                         list(reversed(names)))


if __name__ == "__main__":
    unittest.main()
