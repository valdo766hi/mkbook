import base64
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import lxml.etree as etree

import mkbook

H = mkbook.H
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
BASE = "https://learning.oreilly.com/library/view/demo/123/"
# Shaped like the reader's XMLSerializer output: undeclared epub: prefix, absolute asset paths.
CH01 = (
    '<div xmlns="http://www.w3.org/1999/xhtml" id="sbo-rt-content"><section data-type="chapter" epub:type="chapter">'
    '<h1>One</h1><p id="p1">See <a href="ch02.html#p2">two</a>, <a href="ch09.html#x" target="_blank">nine</a>'
    ' and <a href="https://example.com/">web</a>.</p><script>bad()</script>kept tail'
    '<img src="/api/v2/epubs/urn:orm:book:123/files/assets/a.png" alt="a"/>'
    '<img src="/api/v2/epubs/urn:orm:book:123/files/assets/gone.png" alt="gone"/>'
    '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math></section></div>'
)
CH02 = (
    '<div xmlns="http://www.w3.org/1999/xhtml" id="sbo-rt-content"><section>'
    '<h1 id="p2">Two</h1><p>Back to <a href="ch01.html#p1">one</a>.</p></section></div>'
)


class FakePage:
    """Just enough of a Playwright page for first_section: `links` maps a page URL to the section hrefs on it."""

    def __init__(self, links=None):
        self.links, self.url, self.gotos = links or {}, "about:blank", []

    def goto(self, url, **_):
        self.gotos.append(url)
        self.url = url

    def _match(self, sel):
        book_id = sel.split('"/')[1].split("/")[0]
        hits = [h for h in self.links.get(self.url, []) if f"/{book_id}/" in h and h.endswith(".html")]
        return hits[0] if hits else None

    def wait_for_selector(self, sel, **_):
        if not self._match(sel):
            raise mkbook.PlaywrightError("timeout")

    def eval_on_selector(self, sel, _):
        return self._match(sel)


class MkbookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.work = self.tmp / "work" / "123"
        self.book = {
            "id": "123",
            "base": BASE,
            "title": "Demo Book",
            "authors": ["A. Author", "B. Author"],
            "isbn": "9780000000002",
            "publisher": "Pub",
            "language": "en",
            "css": ["epub.css"],
            "toc": [{"file": "ch01", "title": "One"}, {"file": "ch02", "title": "Two"}],
            "cover": "cover.png",
        }
        mkbook.write(self.work / "book.json", json.dumps(self.book).encode())
        mkbook.write(self.work / "cover.png", PNG)
        mkbook.write(self.work / "files" / "assets" / "a.png", PNG)
        mkbook.write(self.work / "files" / "epub.css", b"#sbo-rt-content p { margin: 0 }")
        mkbook.write(self.work / "sections" / "ch01.xhtml", CH01.encode())

    def test_parse_toc(self):
        links = [
            (BASE, "Demo Book"),
            (BASE + "ch01.html", "One"),
            (BASE + "ch01.html#s1", "Sub"),
            (BASE + "quiz/9/abc/", "Practice Quiz"),
            (BASE + "ch02.html", "Two"),
            ("https://learning.oreilly.com/library/view/other/999/ch03.html", "Other book"),
        ]
        self.assertEqual(
            mkbook.parse_toc(links, BASE, "123"),
            ("Demo Book", [{"file": "ch01", "title": "One"}, {"file": "ch02", "title": "Two"}]),
        )

    def test_section_href(self):
        for url in (BASE + "ch02.html", BASE + "ch02.html#p2", BASE + "ch02.html?x=1"):
            self.assertEqual(mkbook.section_href(url), BASE + "ch02.html")
        self.assertIsNone(mkbook.section_href(BASE))
        self.assertIsNone(mkbook.section_href(BASE + "quiz/9/abc/"))

    def test_first_section(self):
        page = FakePage()
        self.assertEqual(mkbook.first_section(page, BASE + "ch02.html#p2"), (BASE + "ch02.html", "123"))
        self.assertEqual(page.gotos, [BASE + "ch02.html"])

        page = FakePage({BASE[:-1]: [BASE + "foreword01.html"]})
        self.assertEqual(mkbook.first_section(page, BASE), (BASE + "foreword01.html", "123"))
        self.assertEqual(page.gotos, [BASE[:-1], BASE + "foreword01.html"])

        with self.assertRaises(SystemExit):
            mkbook.first_section(FakePage(), "https://example.com/book/")
        with self.assertRaises(SystemExit):
            mkbook.first_section(FakePage(), BASE)

    def test_chapter(self):
        problems = []
        data, props = mkbook.chapter(self.work, self.book, "ch01", "One", {"ch01", "ch02"}, problems)
        doc = etree.fromstring(data)
        text = "".join(doc.itertext())
        hrefs = [a.get("href") for a in doc.iter(H + "a")]

        self.assertEqual(props, ["mathml"])
        self.assertEqual(problems, ["ch01: image gone.png"])
        self.assertIsNone(next(doc.iter(H + "script"), None))
        self.assertIn("kept tail", text)
        self.assertIn("[image missing: gone.png]", text)
        self.assertEqual([i.get("src") for i in doc.iter(H + "img")], ["../assets/a.png"])
        self.assertEqual(hrefs, ["ch02.xhtml#p2", "https://example.com/"])
        self.assertIn("nine", text)  # dead link keeps its text
        self.assertEqual(doc.find(f".//{H}section").get("{http://www.idpf.org/2007/ops}type"), "chapter")
        self.assertEqual([lnk.get("href") for lnk in doc.iter(H + "link")], ["../epub.css", "../mkbook.css"])

    def test_epub_path(self):
        (self.tmp / "books").mkdir()
        self.assertEqual(mkbook.epub_path(None, 'SRE: A/B "2e"'), Path("SRE AB 2e.epub"))
        self.assertEqual(mkbook.epub_path("books", "T"), Path("books/T.epub"))
        self.assertEqual(mkbook.epub_path("new/", "T"), Path("new/T.epub"))
        self.assertEqual(mkbook.epub_path("x/name", "T"), Path("x/name.epub"))
        self.assertEqual(mkbook.epub_path("~/b.epub", "T"), Path.home() / "b.epub")

    @unittest.skipUnless(shutil.which("epubcheck"), "epubcheck not on PATH")
    def test_build(self):
        with self.assertRaises(SystemExit):
            mkbook.build("123")
        self.assertEqual(list(self.tmp.glob("*.epub")), [])

        mkbook.build("123", allow_partial=True, output="out/My Book")
        with zipfile.ZipFile(self.tmp / "out" / "My Book.partial.epub") as z:
            self.assertIn("Missing from this EPUB", z.read("EPUB/nav.xhtml").decode())

        mkbook.write(self.work / "sections" / "ch02.xhtml", CH02.encode())
        mkbook.write(self.work / "files" / "assets" / "gone.png", PNG)
        mkbook.build("123")
        with zipfile.ZipFile(self.tmp / "Demo Book.epub") as z:
            nav = z.read("EPUB/nav.xhtml").decode()
            self.assertLess(nav.index("text/ch01.xhtml"), nav.index("text/ch02.xhtml"))
            self.assertNotIn("Missing", nav)
            self.assertIn('href="ch01.xhtml#p1"', z.read("EPUB/text/ch02.xhtml").decode())
            opf = etree.fromstring(z.read("EPUB/content.opf"))
            spine = opf.find("{http://www.idpf.org/2007/opf}spine")
            self.assertEqual(spine[0].get("idref"), "mkbook-cover")
            self.assertIn('src="../mkbook-cover.png"', z.read("EPUB/text/mkbook-cover.xhtml").decode())


if __name__ == "__main__":
    unittest.main()
