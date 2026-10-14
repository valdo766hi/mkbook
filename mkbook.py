"""Turn an O'Reilly book you can read in your own Chrome session into an EPUB 3.

Give it the book's URL and it pages through the book on its own, saving every
section, and writes the complete EPUB at the end:

    uv run mkbook.py https://learning.oreilly.com/library/view/<book>/<id>/

With no URL it watches the reader tab instead and saves each section as you
display it in Chrome. Either way an interrupted run resumes where it stopped.
"""

import argparse
import base64
import contextlib
import json
import mimetypes
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from xml.sax.saxutils import escape

import lxml.etree as etree
from ebooklib import epub
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

WORK = Path("work")
H = "{http://www.w3.org/1999/xhtml}"
DEVTOOLS_PORT = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
READER = re.compile(r"(https://learning\.oreilly\.com/library/view/[^/]+/([^/]+)/)([^/?#]+)\.html")
BOOK = re.compile(r"(https://learning\.oreilly\.com/library/view/[^/]+/([^/]+))/?$")
ASSET = re.compile(r"/api/v2/epubs/[^/]+/files/(.+)")
SECTION_LINK = re.compile(r"(?:https://learning\.oreilly\.com)?(?:/library/view/[^/]+/[^/]+/)?([\w.-]+)\.html(#.*)?")
CSS_URL = re.compile(r"""url\(\s*['"]?([^'")]+)""")
DROP = ("script", "style", "noscript", "iframe", "form", "button", "input")

PAGE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title>{head}</head>
<body>{body}</body>
</html>"""

CSS = b"""#sbo-rt-content pre { white-space: pre-wrap; overflow-wrap: anywhere; }
#sbo-rt-content img { max-width: 100%; height: auto; }
#sbo-rt-content table { max-width: 100%; }
#sbo-rt-content td, #sbo-rt-content th { overflow-wrap: anywhere; }
.mkbook-missing { color: #b00; font-weight: bold; }
.mkbook-cover { margin: 0; text-align: center; }
.mkbook-cover img { max-width: 100%; max-height: 100vh; }
"""

# Identifies the displayed section's content; the reader updates the URL ~90 ms
# before it swaps the content, so a new section is only taken once this changes.
SIG = """(() => { const r = document.querySelector('#sbo-rt-content');
  return r && r.firstElementChild ? r.textContent.length + ':' + r.textContent.slice(0, 300) : null; })()"""
READY_JS = (
    "prev => { const sig = " + SIG + "; return sig !== null && sig !== prev"
    """ && !!document.querySelector('aside[aria-label="Table of contents"] a[href$=".html"]'); }"""
)

SNAPSHOT_JS = """() => {
  const root = document.querySelector('#sbo-rt-content');
  if (!root || !root.firstElementChild) return null;
  const meta = k => [...document.querySelectorAll(`meta[property="${k}"],meta[name="${k}"]`)]
    .map(m => m.content.trim()).filter(Boolean);
  return {
    xhtml: new XMLSerializer().serializeToString(root),
    images: [...root.querySelectorAll('img[src]')].map(i => i.src),
    css: [...document.querySelectorAll('link[rel=stylesheet]')].map(l => l.href)
      .filter(h => h.includes('/api/v2/epubs/')),
    toc: [...document.querySelectorAll('aside[aria-label="Table of contents"] a[href]')]
      .map(a => [a.href, a.textContent.replace(/\\s+/g, ' ').trim()]),
    authors: meta('og:book:author'),
    isbn: meta('og:book:isbn')[0] || null,
    publisher: meta('publisher')[0] || null,
    cover: meta('og:image')[0] || null,
    language: document.documentElement.lang || 'en',
    pageTitle: document.title,
    href: location.href,
    sig: """ + SIG + """,
  };
}"""

# Runs inside the reader page, so assets are fetched with the page's own session;
# this process never sees cookies or tokens.
FETCH_JS = """async url => {
  try {
    const r = await fetch(url, {credentials: 'same-origin'});
    if (!r.ok) return {error: `HTTP ${r.status}`};
    const bytes = new Uint8Array(await r.arrayBuffer());
    let s = '';
    for (let i = 0; i < bytes.length; i += 32768) s += String.fromCharCode(...bytes.subarray(i, i + 32768));
    return {type: r.headers.get('content-type') || '', data: btoa(s)};
  } catch (e) {
    return {error: String(e)};
  }
}"""


class CaptureError(Exception):
    pass


def asset_path(url):
    m = ASSET.fullmatch(urlparse(url).path)
    return m and m[1]


def section_href(url):
    """The section URL the reader uses for `url` (fragment and query dropped), or None."""
    m = READER.match(urldefrag(url)[0].split("?")[0])
    return m and f"{m[1]}{m[3]}.html"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def fetch(page, url):
    r = {"error": "not attempted"}
    for _ in range(2):
        r = page.evaluate(FETCH_JS, url)
        if "data" in r:
            return base64.b64decode(r["data"]), r["type"]
    print(f"warning: could not fetch {url}: {r['error']}", file=sys.stderr)
    return None


def parse_toc(links, base, book_id):
    """Return (book title, [{file, title}]) from the reader sidebar links, in reading order."""
    section = re.compile(rf"/library/view/[^/]+/{re.escape(book_id)}/([^/]+)\.html")
    title, toc, seen = None, [], set()
    for href, text in links:
        path = urlparse(href).path
        if path == urlparse(base).path:
            title = title or text
        elif (m := section.fullmatch(path)) and m[1] not in seen:
            seen.add(m[1])
            toc.append({"file": m[1], "title": text})
    return title, toc


def reader_page(browser):
    if not browser.is_connected():
        raise CaptureError("lost the connection to Chrome (closed or restarted?); restart capture")
    pages = [p for c in browser.contexts for p in c.pages if READER.match(p.url)]
    # Chrome on macOS reports every tab hidden while another window covers it,
    # so visibility only breaks ties; a single reader tab is always used.
    if len(pages) > 1:
        visible = [p for p in pages if p.evaluate("document.visibilityState") == "visible"]
        if len(visible) != 1:
            urls = "".join(f"\n  {p.url}" for p in pages)
            raise CaptureError(f"{len(pages)} O'Reilly reader tabs are open; close the ones you are not capturing:{urls}")
        pages = visible
    if not pages:
        raise CaptureError("no O'Reilly reader tab is open (…/library/view/<book>/<id>/<section>.html)")
    return pages[0]


def reader_context(browser):
    """The Chrome profile (context) that is logged in to O'Reilly, so a new tab shares its session."""
    for c in browser.contexts:
        if any("learning.oreilly.com" in p.url for p in c.pages):
            return c
    return browser.contexts[0]


def missing(work, book):
    return [t for t in book["toc"] if not (work / "sections" / f"{t['file']}.xhtml").exists()]


def load_book(work):
    """The book's metadata from work/book.json, or {} before the first capture."""
    book_file = work / "book.json"
    try:
        return json.loads(book_file.read_text()) if book_file.exists() else {}
    except (OSError, ValueError) as e:
        raise CaptureError(f"could not read {book_file}: {e}") from e


def capture_section(page, href, prev=None, timeout=10_000):
    """Save the section shown at `href` once its content differs from `prev`; return its signature."""
    m = READER.match(href)
    if not m:
        raise CaptureError(f"not a book section: {href}")
    base, book_id, name = m.groups()
    # polling, not rAF: rAF pauses while Chrome is covered by another window;
    # a timeout is fine: the checks below say what is missing
    with contextlib.suppress(PlaywrightError):
        page.wait_for_function(READY_JS, arg=prev, polling=200, timeout=timeout)
    snap = page.evaluate(SNAPSHOT_JS)
    if not snap:
        raise CaptureError(f"no book content on {name} (logged out?)")
    if urlparse(snap["href"]).path != urlparse(href).path:
        raise CaptureError(f"moved past {name} before it finished loading; go back to it to capture it")
    if snap["sig"] == prev:
        raise CaptureError(f"{name} has not finished loading")
    title, toc = parse_toc(snap["toc"], base, book_id)
    if not toc:
        raise CaptureError("table of contents not found: open the reader's Contents panel")

    work = WORK / book_id
    data = snap["xhtml"].encode()
    # Two real sections never match exactly, so a match means stale content from a fast page turn.
    for other in (work / "sections").glob("*.xhtml"):
        if other.stem != name and other.read_bytes() == data:
            raise CaptureError(f"{name} shows the same content as {other.stem}: still loading, or revisit {other.stem}")
    book = load_book(work)
    book.update(
        id=book_id,
        base=base,
        title=title or snap["pageTitle"].rsplit(" | ", 1)[-1],
        authors=snap["authors"],
        isbn=snap["isbn"],
        publisher=snap["publisher"],
        language=snap["language"],
        css=[p for p in map(asset_path, snap["css"]) if p],
        toc=toc,
    )

    queue = list(dict.fromkeys(snap["images"] + snap["css"]))
    for url in queue:  # grows with url() references found in stylesheets
        path = asset_path(url)
        dest = path and work / "files" / path
        if not dest:
            print(f"warning: not a book asset, skipped: {url}", file=sys.stderr)
            continue
        if not dest.exists():
            got = fetch(page, url)
            if not got:
                continue
            write(dest, got[0])
        if dest.suffix == ".css":
            refs = (urljoin(url, r) for r in CSS_URL.findall(dest.read_text()) if not r.startswith(("data:", "#")))
            queue += [u for u in refs if u not in queue]

    if not book.get("cover") and snap["cover"] and (got := fetch(page, snap["cover"])):
        ext = {"image/jpeg": "jpg", "image/png": "png"}.get(got[1].split(";")[0])
        if ext:
            write(work / f"cover.{ext}", got[0])
            book["cover"] = f"cover.{ext}"

    write(work / "sections" / f"{name}.xhtml", data)
    write(work / "book.json", json.dumps(book, indent=1, ensure_ascii=False).encode())
    label = next((t["title"] for t in toc if t["file"] == name), None)
    note = f'"{label}"' if label else "(not in the table of contents; skipped in the EPUB)"
    print(f"[{len(toc) - len(missing(work, book))}/{len(toc)}] {name} {note}")
    return snap["sig"]


def epub_path(output, title):
    """Resolve -o (a file, a directory, or nothing) to the EPUB path."""
    name = (re.sub(r'[\\/:*?"<>|]+', "", title).strip() or "book") + ".epub"
    if not output:
        return Path(name)
    out = Path(output).expanduser()
    if out.is_dir() or output.endswith("/"):
        return out / name
    return out if out.suffix == ".epub" else out.with_name(out.name + ".epub")


def announce(book, left, output):
    """Report progress, ask for the EPUB path unless -o was given, and return it."""
    total = len(book["toc"])
    print(f'"{book['title']}": {total - left}/{total} sections captured.')
    if output is None:
        default = epub_path(None, book["title"])
        try:
            output = input(f"Save EPUB as [{default}]: ").strip() or str(default)
        except EOFError:
            output = str(default)
    if left:
        print("Now go through the book in Chrome (Contents panel or the next arrow); each section is saved as it appears.")
        print("Ctrl-C stops early; run again later to continue where you left off.")
    return output


def stop(signum, frame):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C can arrive twice (terminal + uv); let the build finish
    raise KeyboardInterrupt


def start_link(page, book_id):
    """The book page's link into the reader (its Start button)."""
    sel = f'a[href*="/{book_id}/"][href$=".html"]'
    try:
        page.wait_for_selector(sel, timeout=30_000)
    except PlaywrightError:
        sys.exit("the book page links to no section: open the book's first section in Chrome once, then rerun")
    return page.eval_on_selector(sel, "a => a.href")


def first_section(page, url):
    """Open `url` (a book or a section page) at the book's first section; return (href, book_id)."""
    clean = urldefrag(url)[0].split("?")[0]
    if m := READER.match(clean):
        href, book_id = f"{m[1]}{m[3]}.html", m[2]
    else:
        if not (m := BOOK.fullmatch(clean)):
            sys.exit(f"not an O'Reilly book URL: {url}\n(give .../library/view/<book>/<id>/ or any section page)")
        page.goto(m[1], wait_until="load", timeout=60_000)
        book_id = m[2]
        href = section_href(page.url) or start_link(page, book_id)  # some book URLs land in the reader directly
    page.goto(href, wait_until="load", timeout=60_000)
    return href, book_id


def visit(page, href, prev):
    """Open the section at `href` in the capture tab and capture it; retry once on a transient failure."""
    try:
        page.goto(href, wait_until="load", timeout=60_000)
        return capture_section(page, href, prev, timeout=30_000)
    except (CaptureError, PlaywrightError) as e:
        print(f"retrying: {e}", file=sys.stderr)
        page.goto(href, wait_until="load", timeout=60_000)
        return capture_section(page, href, prev, timeout=30_000)


def auto(browser, url, output):
    """Page through the whole book at `url` in a dedicated tab, capturing every section."""
    require_epubcheck()
    page = reader_context(browser).new_page()
    book_id = None
    try:
        href, book_id = first_section(page, url)
        prev = capture_section(page, href, None, timeout=30_000)
        book = load_book(WORK / book_id)
        left = missing(WORK / book_id, book)
        print(f'"{book["title"]}": {len(book["toc"]) - len(left)}/{len(book["toc"])} sections captured.')
        if left:
            print(f"Capturing the remaining {len(left)} automatically; the EPUB will be {epub_path(output, book['title'])}.")
        for t in left:
            time.sleep(1.0)  # keep the pace of a person reading, not a crawler
            prev = visit(page, f"{book['base']}{t['file']}.html", prev)
    except (CaptureError, PlaywrightError) as e:
        sys.exit(f"automatic capture stopped: {e}\nRun mkbook.py again with the same URL to resume.")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        with contextlib.suppress(PlaywrightError):
            page.close()
    return book_id, output


def manual(browser, output):
    """Watch the reader tab and capture each section as the user pages through the book."""
    book_id = last = prev = noted = None
    started = False
    try:
        while True:
            try:
                page = reader_page(browser)
                # page.url can lag behind the reader's in-page navigation; location.href cannot
                href = page.evaluate("location.href")
                if not (m := READER.match(href)):
                    raise CaptureError("the reader tab is not on a book section")
                tab_book, name = m[2], m[3]
                if book_id and tab_book != book_id:
                    raise CaptureError(f"the open tab is another book ({tab_book}); this run is capturing {book_id}")
                if name != last:
                    prev, last, book_id = capture_section(page, href, prev), name, tab_book
                    book = load_book(WORK / book_id)
                    left = len(missing(WORK / book_id, book))
                    if not started:
                        output, started = announce(book, left, output), True
                    if not left:
                        break
                noted = None
            except CaptureError as e:
                if str(e) != noted:
                    print(f"waiting: {e}", file=sys.stderr)
                    noted = str(e)
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nstopped")
    return book_id, output


def run(output=None, allow_partial=False, book=None):
    signal.signal(signal.SIGINT, stop)
    try:
        port, path = DEVTOOLS_PORT.read_text().split()[:2]
    except (OSError, ValueError):
        sys.exit("Chrome remote debugging is off: enable it at chrome://inspect/#remote-debugging")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}{path}", timeout=120_000)
        except PlaywrightError as e:
            sys.exit(f"cannot attach to Chrome (click Allow on its remote debugging prompt): {e}")
        book_id, output = auto(browser, book.strip(), output) if book else manual(browser, output)
    if book_id:
        build(book_id, allow_partial, output)


def chapter(work, book, name, title, included, problems):
    """Wrap a captured section as an EPUB XHTML document; return (bytes, manifest properties)."""
    css = [c for c in book["css"] if (work / "files" / c).exists()] + ["mkbook.css"]
    head = "".join(f'<link rel="stylesheet" type="text/css" href="../{c}"/>' for c in css)
    body = (work / "sections" / f"{name}.xhtml").read_text()
    doc = etree.fromstring(PAGE.format(lang=book["language"], title=escape(title), head=head, body=body).encode())
    etree.strip_elements(doc, *(H + t for t in DROP), with_tail=False)

    page_url = f"{book['base']}{name}.html"
    for img in list(doc.iter(H + "img")):
        src = urljoin(page_url, img.get("src", ""))
        path = asset_path(src)
        if path and (work / "files" / path).exists():
            img.set("src", f"../{path}")
        else:
            problems.append(f"{name}: image {posixpath.basename(urlparse(src).path)}")
            img.tag, img.text = H + "span", f"[image missing: {posixpath.basename(urlparse(src).path)}]"
            img.attrib.clear()
            img.set("class", "mkbook-missing")

    for a in list(doc.iter(H + "a")):
        m = SECTION_LINK.fullmatch(a.get("href", ""))
        if not m:
            continue
        if m[1] in included:
            a.set("href", f"{m[1]}.xhtml{m[2] or ''}")
        else:  # target section is not in this book; keep the text, drop the dead link
            a.tag = H + "span"
            for k in ("href", "target", "rel"):
                a.attrib.pop(k, None)

    props = [
        p
        for p, tag in (("mathml", "{http://www.w3.org/1998/Math/MathML}math"), ("svg", "{http://www.w3.org/2000/svg}svg"))
        if next(doc.iter(tag), None) is not None
    ]
    return etree.tostring(etree.ElementTree(doc), xml_declaration=True, encoding="utf-8", doctype="<!DOCTYPE html>"), props


def require_epubcheck():
    if not shutil.which("epubcheck"):
        sys.exit("epubcheck not found: run inside the devenv shell (direnv allow, or nix develop --no-pure-eval)")


def build(book_id, allow_partial=False, output=None):
    require_epubcheck()
    work = WORK / book_id
    try:
        book = load_book(work)
    except CaptureError as e:
        sys.exit(str(e))
    have = {p.stem for p in (work / "sections").glob("*.xhtml")}
    for name in sorted(have - {t["file"] for t in book["toc"]}):
        print(f"warning: skipping {name}: not in the table of contents", file=sys.stderr)
    sections = [t for t in book["toc"] if t["file"] in have]
    included = {t["file"] for t in sections}
    if not sections:
        sys.exit(f"no captured sections in {work}")

    problems = [f"section: {t['title']}" for t in missing(work, book)]
    docs = [(t, *chapter(work, book, t["file"], t["title"], included, problems)) for t in sections]
    if problems:
        print("incomplete book:\n  " + "\n  ".join(problems), file=sys.stderr)
        if not allow_partial:
            sys.exit("not building: run again to capture the rest, or pass --allow-partial")

    eb = epub.EpubBook()
    eb.set_identifier(f"urn:isbn:{book['isbn']}" if book.get("isbn") else f"urn:oreilly:{book_id}")
    eb.set_title(book["title"])
    eb.set_language(book["language"])
    for i, author in enumerate(book["authors"]):
        eb.add_author(author, uid=f"creator{i}")
    if book.get("publisher"):
        eb.add_metadata("DC", "publisher", book["publisher"])

    files = sorted(f for f in (work / "files").rglob("*") if f.is_file() and not f.name.endswith(".tmp"))
    for i, f in enumerate(files):
        media_type = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        eb.add_item(epub.EpubItem(f"asset{i}", f.relative_to(work / "files").as_posix(), media_type, f.read_bytes()))
    eb.add_item(epub.EpubItem("mkbook-css", "mkbook.css", "text/css", CSS))

    spine, toc = [], []
    if book.get("cover"):
        cover = f"mkbook-{book['cover']}"
        # ebooklib's own cover page hard-codes lang="en" and has no <title>, so write one
        eb.set_cover(cover, (work / book["cover"]).read_bytes(), create_page=False)
        alt = escape(book["title"], {'"': "&quot;"})
        body = f'<div class="mkbook-cover" epub:type="cover"><img src="../{cover}" alt="{alt}"/></div>'
        head = '<link rel="stylesheet" type="text/css" href="../mkbook.css"/>'
        page = PAGE.format(lang=book["language"], title="Cover", head=head, body=body)
        item = epub.EpubItem("mkbook-cover", "text/mkbook-cover.xhtml", "application/xhtml+xml", page.encode())
        eb.add_item(item)
        spine.append(item)
        eb.guide.append({"type": "cover", "href": item.file_name, "title": "Cover"})
    for t, data, props in docs:
        item = epub.EpubItem(f"s-{t['file']}", f"text/{t['file']}.xhtml", "application/xhtml+xml", data)
        # ebooklib's OPF writer reads this undeclared attribute via hasattr
        item.properties = props  # pyright: ignore[reportAttributeAccessIssue]
        eb.add_item(item)
        spine.append(item)
        toc.append(epub.Link(item.file_name, t["title"], f"toc-{t['file']}"))
    if problems:
        items = "".join(f"<li>{escape(p)}</li>" for p in problems)
        body = f"<h1>Missing from this EPUB</h1><p>This book is incomplete. Not captured:</p><ul>{items}</ul>"
        page = PAGE.format(lang=book["language"], title="Missing from this EPUB", head="", body=body)
        item = epub.EpubItem("mkbook-missing", "text/mkbook-missing.xhtml", "application/xhtml+xml", page.encode())
        eb.add_item(item)
        spine.append(item)
        toc.append(epub.Link(item.file_name, "Missing from this EPUB", "toc-missing"))
    eb.toc = toc
    eb.spine = spine
    eb.add_item(epub.EpubNcx())
    eb.add_item(epub.EpubNav())

    out = epub_path(output, book["title"])
    if problems:
        out = out.with_name(f"{out.stem}.partial{out.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.tmp.epub")
    epub.write_epub(str(tmp), eb, {"epub3_pages": False})
    check = subprocess.run(["epubcheck", str(tmp)], capture_output=True, text=True)
    print((check.stdout + check.stderr).strip())
    if check.returncode:
        bad = out.with_name(f"{out.stem}.invalid.epub")
        tmp.replace(bad)
        sys.exit(f"epubcheck failed; kept {bad} for inspection")
    tmp.replace(out)
    print(f"wrote {out}: {len(docs)}/{len(book['toc'])} sections" + (" (PARTIAL)" if problems else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book", nargs="?", metavar="url", help="O'Reilly book or section URL: every section is captured automatically")
    ap.add_argument("-o", "--output", help="EPUB file name or directory (defaults to <Title>.epub)")
    ap.add_argument("--allow-partial", action="store_true", help="on Ctrl-C, still write <name>.partial.epub")
    args = ap.parse_args()
    run(args.output, args.allow_partial, args.book)


if __name__ == "__main__":
    main()
