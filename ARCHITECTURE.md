# mkbook — Architecture

Turns an O'Reilly book read in the user's logged-in Chrome into one EPUB 3 file, either by paging through it automatically in a dedicated tab or by capturing the sections the user opens. Everything runs locally and the result is for personal use only.

## Reconnaissance findings (2026-10-13, live Chrome session via Browser Use)

Pages inspected: the home page, the book page for *AI Engineering* (`9781098166298`), `preface01.html` and `ch05.html`. Nothing was downloaded.

| Area | Observed |
|---|---|
| Session | Chrome 155 is logged in to `learning.oreilly.com`. Once remote debugging is enabled at `chrome://inspect/#remote-debugging`, CDP is reachable only by websocket: `/json/version` returns 404, so the endpoint is read from Chrome's `DevToolsActivePort` file. |
| Book metadata | `og:book:author`, `og:book:isbn`, `meta[name=publisher]`, `html[lang]`, plus the cover at `og:image` (`/covers/urn:orm:book:<isbn>/`). On reader pages `og:title` is the *section* title; the book title comes from the TOC's link to the book page. |
| URLs | Book page: `/library/view/<slug>/<isbn>/`. Each section has its own URL: `…/<isbn>/preface01.html`, `ch01.html` … `ch10.html`, `afterword01.html`, `ix01.html`, `colophon01.html`. |
| TOC | `aside[aria-label="Table of contents"]` lists every section as `…/<isbn>/<file>.html`, in reading order, with `#fragment` links for subsections and some `quiz/` links. *AI Engineering* has 14 sections. |
| Content container | `#sbo-rt-content`, inside `#book-content`, which is inside the React reader UI (`main#main`). It has no scripts or buttons inside, and holds the publisher's HTMLBook XHTML: `section[data-type=chapter]`, `epub:type`, and `data-pdf-bookmark`. |
| Whole section in DOM | Yes. `ch05` gives 78k characters in one page with no pagination. Images use plain `src` with no `loading` attribute, but only 11 of 16 had loaded before scrolling. |
| Elements (ch05) | Headings use `h1`–`h3`, with `h1` repeated per `sect1`. There are 16 `figure > div.figure > img + h6` captions (no `figcaption`), 13 `pre[data-type=programlisting]` (plain text with `<strong>`, no highlight spans), 4 `table` with `caption`, and 22 `p[data-type=footnote]`. There is no MathML in this book. |
| Assets | Image `src` is `/api/v2/epubs/urn:orm:book:<isbn>/files/assets/<name>.png`. The book CSS is at `…/files/epub.css`: every selector is scoped to `#sbo-rt-content`, and it references `css_assets/titlepage_footer_ebook.png`. |
| Links | Internal links look like `ch04.html#id`, `ch05.html#id1134` or `#id`. External links use `oreil.ly` short links. |
| Fresh tab (2026-10-14) | `goto(<section>.html)` in a new tab renders `#sbo-rt-content`, and the Contents aside is in the DOM with every TOC link even while hidden. A load takes ~3.5 s plus ~1 s until ready. The book page has exactly one section link, its Start button. |

## Chosen approach

```text
uv run mkbook.py [URL] [-o PATH]  ── Playwright connect_over_cdp(ws from DevToolsActivePort)
        |   URL given: a new tab in the logged-in profile goto()s each uncaptured TOC section in turn
        |   no URL:    watch the user's reader tab; whenever it shows a new section
        |   either way, read the section:
        |     - book meta + TOC
        |     - XMLSerializer(#sbo-rt-content)
        |     - its images + book CSS via in-page fetch (same session; cookies never read)
        v
work/<isbn>/{book.json, cover.<ext>, sections/<name>.xhtml, files/<publisher path>}
        |
        v
all sections captured (or Ctrl-C)  ── chapter() → ebooklib → EPUB 3 → epubcheck
        v
<Title>.epub   |  <Title>.partial.epub (only with --allow-partial)  |  <Title>.invalid.epub
```

| Alternative | Verdict |
|---|---|
| `api/v2/epubs` manifest download (safaribooks-style) | Not used: it downloads the book outside the reader. mkbook calls `…/files/` only for the images and CSS of sections it captures. |
| Firefox / Zen | Firefox removed CDP in v141, and the session lives in Chrome anyway. |
| Save Page As + parser | Works, but each section needs a dialog and the saved DOM carries all the UI. |
| Readability | Not needed: the container is known and already semantic. |
| pandoc | Re-parses the HTMLBook markup and drops `data-type` and `epub:type`. ebooklib packages the XHTML unchanged. |

## Stack

Python 3.12 in one file, `mkbook.py`, run as a single command: `uv run mkbook.py [URL] [-o PATH] [--allow-partial]`. The environment comes from `flake.nix` + `devenv.nix`, loaded with direnv (`.envrc`). It provides Python, uv and epubcheck, and runs `uv sync --frozen` from `pyproject.toml` / `uv.lock`.

- `playwright` 1.63: only `connect_over_cdp`, with no bundled browser download.
- `ebooklib` 0.20: OPF, nav, NCX and zip. Chapters are added as plain `EpubItem`s so ebooklib writes them unchanged.
- `lxml` 6.1: cleaning. ebooklib already depends on it.
- `epubcheck` 5.3.0: validation, from nixpkgs through devenv.

## Components

| Function | Responsibility |
|---|---|
| `run()` | Read Chrome's `DevToolsActivePort`, attach, and run `auto()` when a URL is given, else `manual()`. Both stop on Ctrl-C (a second, forwarded SIGINT is ignored) and return the captured book, which `build()` then packages. An auto-mode abort exits before any build. |
| `auto()` | Open a dedicated tab in the O'Reilly-logged-in profile (`reader_context()`), resolve the URL to a section (`first_section()`: a section URL is used as is; a book page is followed through its Start link, `start_link()`), capture it to learn the TOC, then `visit()` every missing section with a 1 s pause: `goto` plus `capture_section()` (30 s wait), retried once. The hidden Contents aside is in the DOM on a fresh load, so no sidebar clicks are needed. Any further failure exits with a resume hint; the tab is always closed. Never prompts: without `-o` the EPUB goes to `<Title>.epub`. |
| `manual()` | Poll the reader tab (`reader_page()`) every 0.3 s. If several reader tabs are open, pick the visible one (Chrome on macOS reports all tabs hidden while another window covers it). The section comes from the page's `location.href`, because Playwright's `page.url` lagged behind in-page navigation and a Part I page was once saved as the foreword. After the first capture, report progress and ask for the EPUB path unless `-o` was given (`announce()`). Stop when every TOC section is captured. |
| `capture_section()` | The reader updates the URL ~90 ms before it swaps in the content (measured 2026-10-13), so wait up to 10 s (30 s in auto mode) for content that differs from the previous capture. Then refuse if `#sbo-rt-content` or the TOC is missing, if the location moved on, or if the content is identical to another saved section (stale content from a fast page turn). Otherwise save `XMLSerializer` output of `#sbo-rt-content`; fetch the section's images and CSS (plus CSS `url()` refs) with an in-page `fetch`, skipping files already saved; save the cover once; update `book.json`. Print `[2/40] ch01 "1. Introduction: …"`. |
| `parse_toc()`, `missing()`, `load_book()` | Book title and section list from the TOC links (subsection fragments and `quiz/` links dropped); the TOC sections without a saved file; `book.json`, or `{}` before the first capture. |
| `chapter()` | Wrap the captured `#sbo-rt-content` (kept as the wrapper so the scoped publisher CSS applies) in an XHTML page that declares the `epub:` prefix. Drop `script`, `style`, `noscript`, `iframe`, `form`, `button` and `input`, keeping their tail text. Images: `/api/v2/epubs/…/files/<path>` → `../<path>`; a missing file becomes a visible `[image missing: name]`. Links: `chNN.html#id` → `chNN.xhtml#id`; a link to a section not in the book becomes a `span` with the same text. Everything else is unchanged. |
| `build(isbn)` | Order sections by the TOC in `book.json`, check coverage, and add one XHTML per section under `text/`, with `mathml`/`svg` properties when needed. Add metadata (title, authors, `urn:isbn:` identifier, language, publisher), the cover image (as `cover-image` metadata, plus a cover page first in the spine, marked `cover` in the guide and landmarks), the publisher assets, `mkbook.css` and the nav/NCX from TOC titles. Write to `*.tmp.epub`, then rename it once epubcheck passes; any epubcheck error exits non-zero. The path comes from `epub_path()`: `-o` as a file, a directory, or nothing (`<Title>.epub`). |

### Formatting

- **Book CSS:** the book's own `epub.css`, fetched as an asset of the displayed section, plus a small `mkbook.css` override: `pre` wraps (`pre-wrap`), `img` scales to the page width, and tables are capped at the page width, and the cover image is centered and fits the screen. The publisher's own `table code { word-break: break-all }` is kept, so code inside table cells breaks mid-word, as it does on O'Reilly.
- **Code:** kept exactly as the publisher wrote it. The publisher uses no highlighting here, so nothing new is added.
- **Figure captions:** stay as `h6` inside `figure`, as published.
- **Footnotes:** the `noteref` and `footnote` pairs stay inside their own chapter, so the links resolve.

## Failure handling

| Case | Behavior |
|---|---|
| No `DevToolsActivePort`, or the attach is refused | Tell the user to enable `chrome://inspect/#remote-debugging` and click Allow. |
| epubcheck not on `PATH` | Exit before capturing in auto mode, or at build in watch mode, and point to the dev shell. |
| Auto mode: URL is not an O'Reilly book or section | Exit, showing the accepted URL shapes. |
| Auto mode: book page has no Start link within 30 s | Exit and ask the user to open the first section once. |
| Watch mode: no reader tab, or several with none clearly in front | Refuse, list the open reader tabs, and capture nothing. |
| Chrome closed or restarted during capture | "Lost the connection to Chrome"; run again. |
| Content identical to another saved section | Refuse it, and name the other section to revisit if that one was the bad save. |
| Login, paywall or expired session (no container) | "No book content on <section> (logged out?)". Watch mode retries on its own once the page is fixed; auto mode exits as below. |
| Watch mode: Contents panel closed (it is removed from the DOM after closing, verified 2026-10-13) | After the 10 s wait, refuse and ask the user to open Contents. |
| Watch mode: tab switches to another book mid-run | Ignored until it is back on the book being captured. |
| Asset request fails (network or 4xx) | Retry once, then warn. The build sees the missing file and marks the book incomplete. |
| Same section captured again | Overwritten; the latest capture wins. |
| Captured section not in the TOC | Warned at capture, and skipped at build. |
| Sections or images missing | Exit 1 and list them. `--allow-partial` writes `*.partial.epub` with a "Missing from this EPUB" page. |
| epubcheck error | Exit 1, print the report, and keep the file as `*.invalid.epub`. |
| Capture interrupted | Each section is written atomically, so rerunning (the same URL in auto mode) resumes. |
| Auto mode: a section fails to load or capture | Retried once (the first section, which supplies the TOC, is not); then exit 1 with a resume hint, and nothing is built. |
| Rate limit | One section at a time: human page turns, or in auto mode sequential `goto`s with a 1 s pause between them. |

## Security

- The tool never reads, prints or stores cookies, tokens or passwords. Asset fetches run as an in-page `fetch` in the reader or capture tab.
- The process itself connects to `localhost` only; every O'Reilly request is made by Chrome. `build` makes no network calls at all.
