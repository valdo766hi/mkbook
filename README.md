# mkbook

Turns an O'Reilly book you can read in your own logged-in Chrome into a single EPUB 3 file, for personal offline reading. Everything runs locally: mkbook attaches to Chrome over the DevTools protocol, saves each section's reading content (no site UI), and packages it with the book's own CSS, images, cover and table of contents. Every build is validated with epubcheck.

## Requirements

- macOS with Google Chrome, logged in to `learning.oreilly.com`.
- Chrome remote debugging enabled at `chrome://inspect/#remote-debugging`. Click **Allow** when Chrome asks on the first attach.
- Nix with direnv. The dev shell (`flake.nix` + `devenv.nix`) provides Python 3.12, uv and epubcheck, and installs the Python dependencies from `uv.lock`.

```sh
direnv allow    # or: nix develop --no-pure-eval
```

## Usage

```sh
# Automatic: pages through the whole book in a new Chrome tab, then writes the EPUB
uv run mkbook.py "https://learning.oreilly.com/library/view/<book>/<id>/" -o ~/Books/

# Watch mode: go through the book in Chrome yourself; each section is saved as it appears
uv run mkbook.py -o ~/Books/
```

| Option | Meaning |
|---|---|
| `url` | A book page or any section page. Omit it for watch mode. |
| `-o PATH` | EPUB file name or directory. Defaults to `<Title>.epub` in the current directory; watch mode asks when it is omitted. |
| `--allow-partial` | On Ctrl-C (or with sections missing), still write `<Title>.partial.epub` with a "Missing from this EPUB" page. |

- The EPUB always contains the whole book, in table-of-contents order, starting with a cover page. Your reading position on O'Reilly is not used.
- Captured sections are kept under `work/<id>/`. A rerun skips what is already saved, so an interrupted run resumes where it stopped.
- If epubcheck reports errors, the file is kept as `<Title>.invalid.epub` and the run exits non-zero.

## Tests

```sh
uv run python -m unittest -v
```

The build tests need epubcheck on `PATH`, which the dev shell provides.

## Known limitations

- Chrome only, and only the default profile's `DevToolsActivePort` path on macOS. Firefox and Zen have no CDP.
- The EPUB navigation lists sections, not subsections.
- The language comes from the reader page's `html[lang]`, which is the site's UI language and not necessarily the book's.
- If O'Reilly changes the reader page, mkbook fails with an error instead of guessing.
- Books with MathML, highlighted code or video sections are untested. The output has been checked with epubcheck and in Chrome, not yet in Apple Books, Calibre or on a Kindle.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works, the design choices and how failures are handled.
