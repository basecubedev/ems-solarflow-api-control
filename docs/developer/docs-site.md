# Documentation website

The pages under `docs/` are also published as a website on GitHub Pages, at
<https://basecubedev.github.io/ems-solarflow-api-control/>. It adds navigation
tabs, full-text search and a dark mode. [Zensical](https://zensical.org/) builds
it from the same Markdown that GitHub shows, so `docs/` stays the only source.
Write pages for GitHub as before; a link that works there works on the site.
The one known exception is the second of two identical headings, which GitHub
anchors as `#name-1` and the site as `#name_1`. The build fails on such a link
rather than publishing it broken.

## Build and preview locally

```bash
python3 -m pip install -r requirements-docs.txt
python3 scripts/docs_site.py stage
zensical serve                     # http://localhost:8000/ems-solarflow-api-control/
```

`zensical serve` watches the staging directory, not `docs/`, so rerun `stage`
after editing a page. CI runs the full sequence:

```bash
python3 scripts/docs_site.py stage
zensical build --clean --strict
python3 scripts/docs_site.py check
```

The stage and the site land under `build/` and Zensical keeps its cache in
`.cache/`; git and Docker ignore both.

## What the script does

`stage` copies the files under `docs/` that git tracks or would track to the
staging directory `docs_dir` in [`zensical.toml`](../../zensical.toml).
Gitignored notes such as `docs/develop/` stay out. While copying it rewrites
every link outside code that the site could not serve. That covers inline
links and images, reference definitions, and `href`, `src` and `poster` in raw
HTML:

- A link to a file outside `docs/` goes to that file on GitHub. Images use the
  raw URL so they still render.
- A link to a directory goes to its `index.md` or `README.md` when it has one
  inside `docs/`, and to the directory on GitHub otherwise.
- A link that leaves `docs/` and comes back, such as `../docs/user/faq.md`, is
  written relative to the page, because the stage is not called `docs`.

The branch is the one named in `edit_uri`, so source links and the "Edit this
page" button point at the same tree. A link to a file that does not exist is
left alone; `zensical build --strict` and
[`tests/test_docs_local_links.py`](../../tests/test_docs_local_links.py) report
it.

`check` reads every built page and fails for each local `href`, `src` or
`poster` that names no file of the site, or names an anchor that page does not
have.

Two settings in `zensical.toml` keep a page that reads right on GitHub right on
the site:

- `use_directory_urls = false` serves a page as `<page>.html` beside the others,
  so a relative path in raw HTML, such as a `<video poster>`, resolves as it
  does on GitHub.
- `toc.slugify` uses `pymdownx.slugs.slugify`, which builds heading anchors the
  way GitHub does, double dashes included.

## Adding a page

Add the page to `nav` in `zensical.toml`. A page that opens a new topic also
belongs in the map in [docs/README.md](../README.md), which lists section
entry points rather than every page.
[`tests/test_docs_site.py`](../../tests/test_docs_site.py) fails for a page
under `docs/` that the navigation leaves out, unless it is a redirect stub
(`# Moved`) or on that test's short list of asset notes.

## Publishing

Two workflows run the same build:

- [`.github/workflows/docs-site.yml`](../../.github/workflows/docs-site.yml)
  (**Repo docs site**) builds and checks the site on every pull request. Its
  **Repo docs site gate** job is the one name to require.
- [`.github/workflows/docs-site-publish.yml`](../../.github/workflows/docs-site-publish.yml)
  (**Repo docs site publish**) builds the site on every push to `main` and
  deploys it to GitHub Pages. It can also be started by hand, but only a run
  on `main` deploys.

GitHub Pages has to be switched on once in the repository settings, under
**Settings → Pages → Build and deployment → Source: GitHub Actions**. Until
then the deploy job fails, while the build job still reports whether the site
builds.
