<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 7 — the `insyn.malmgren.dev` frontend
## 16. Frontend

### 16.0 Where it lives, and why not in the portfolio repo

The frontend is **new code in this repo** (`frontend/`), published to GitHub
Pages on **`insyn.malmgren.dev`**. `www.malmgren.dev` gets one link to it from
`projects.html`, and nothing else changes over there.

This amends §0's "Frontend hosting — stays on GitHub Pages, unchanged" and
§1.2. Still Pages; a different subdomain on a different repo.

Three reasons the portfolio repo is the wrong home, in descending order of
force:

1. **`ErikMalmgren.github.io` serves its repo root.** Its own `CLAUDE.md`:
   *"every committed file is served at www.malmgren.dev."* Committing
   `company/<LEI>.json` hourly means ~700 files and a few MB of churn in a repo
   built to hold eight hand-written pages.
2. **That repo has no build step, by design**, and says to migrate to Hugo past
   ~10 pages. A data app needs neither Hugo nor to be counted against that
   budget.
3. Its enhancement contract (*"complete and readable with JavaScript
   disabled"*) is written for a CV a recruiter must be able to read on a
   locked-down browser. **That constraint is lifted for this app** — it is a
   data explorer, JavaScript is the point — but lifting it *inside* the
   portfolio would erode the rule where it still matters.

Keeping the frontend here also keeps the JSON **same-origin**: no CORS, and
`API_BASE = './data'` from §9 holds literally.

### 16.1 Stack

Hand-written HTML, CSS and **ES modules**. No framework, no bundler, no
`package.json`, no dependencies — the same discipline `malmgren.dev` keeps.

ES modules rather than the portfolio's single IIFE: this is four scripts, not
one, and `import` works natively off a static server with no build step. That
is the only deliberate divergence in idiom.

**Do not port `js/shell.js`.** It is built on a different content model — it
`fetch`es an HTML *page* and prints its `<main>`. This app renders from JSON.
Reusing it would mean pre-rendering every view to HTML, which is the build step
we just avoided. Take the *look*, not the machinery.

### 16.2 How the frontend and the JSON meet

**Do not change `export-static --out dist/`.** `tests/test_api.py` diffs every
API route against its `dist/` file and `doctor`'s `_static_export_checks` reads
`companies.json` / `leaderboard-30d.json` at the export root. Moving the target
breaks both.

Assemble the published artifact in the workflow instead:

```sh
uv run insyn export-static --out dist/
mkdir -p site/data          # both levels: `cp -r src/. dst/` needs dst to exist
cp -r frontend/. site/
cp -r dist/.     site/data/
# upload-pages-artifact  path: site/
```

Result:

```
site/index.html  company.html  about.html  404.html  CNAME
     css/insyn.css   js/*.js
     data/meta.json  companies.json  leaderboard-*.json  company/<LEI>.json
```

`cp -r dist/.` copies dotfiles too. On CI that is nothing — `export_static`
creates `dist/` fresh — but a local `dist/.gitignore` (a common belt-and-braces
addition) would be published. Verified: the four lines above run clean from an
empty tree.

⚠ **Never copy the frontend into `dist/`.** `export_static.export()` opens with
`shutil.rmtree(out)` — it would delete the frontend on the next run. The
one-way copy into `site/` is why this is safe; do not "simplify" it back.

`frontend/` is tracked; `dist/` and `site/` stay gitignored.

### 16.3 Pages

Four, matching the portfolio's instinct to keep the count low.

| Page | Reads | Shows |
| --- | --- | --- |
| `index.html` | `leaderboard-{30d,90d,365d,all}.json` | the leaderboard — period tabs, sortable columns, filter box |
| `company.html?lei=…` | `company/<LEI>.json`, `companies.json` | one company: totals per period, name variants, recent transactions |
| `about.html` | `meta.json`, `data-quality.json` | what the data is, coverage, counting rules, outliers |
| `404.html` | — | served by Pages for unknown paths |

`?lei=` as a plain query string, not hash routing — Pages serves
`company.html?lei=X` fine, and the URL stays copyable.

Nav on every page: `leaderboard  company  about` plus a link back to
`malmgren.dev`. Prompt string `erik@insyn:~$`, so it reads as the same house.

**`index.html` — the leaderboard.** One `fetch` per period, cached in memory
after the first. All eleven sortable fields ship in every entry (§9), so period
switching, sorting and filtering are pure client work with no round trip.

Columns: name · ticker · net · bought · sold · tx · buyers · sellers · market
cap · % of mcap · verification. Every one sortable. On narrow viewports show
name / net / tx / % of mcap and let the rest scroll.

**`company.html` — the detail page.** Header (name, ticker, LEI, primary ISIN,
market cap + `market_cap_as_of`, verification), `name_variants` when there is
more than one, the `periods` table, then `recent_transactions` with
`tx_count_total` stated so a capped list is never mistaken for the whole
history.

**`about.html`.** Coverage and freshness straight from `meta.json`
(`coverage_first_day`..`coverage_last_day`, `missing_days`,
`longest_gap_days`, `last_ingest_at`); the counting rule (only `Förvärv` /
`Avyttring`, everything else excluded with a reason — §6.1); SEK conversion at
the transaction-date rate (§6.3); the `data-quality.json` outlier table, each
row linking to its `fi_search_url`. Render `unmapped_natures` **loudly** if it
is ever non-empty — an empty object is the normal state (invariant 7).

### 16.4 Design system

Copy the `:root` token block and the base rules (`body`, `.wrap`, links,
`:focus-visible`, `.cmd`, `.prompt`, `.section-head`, `.muted`, `.nav`,
`.kv`, `code`) out of `malmgren.dev/css/terminal.css` into
`frontend/css/insyn.css`. **A copy, not a share** — no build step, no
submodule, and two small repos drifting slightly is cheaper than coupling them.
Say so in a comment at the top of the file, with the date copied.

Carry over unchanged: dark default with a full `prefers-color-scheme: light`
inversion, `ui-monospace` stack, and **nothing outside `:root` hardcodes a
colour**.

**The one real tension: `--col` is a measure for prose, and an eleven-column
table does not fit in 96ch.** Do not raise `--col`. Keep it for the text pages
and give the table its own wider container with `overflow-x: auto`, so the
column rules that make `about.html` readable are not bent to suit a table.

Accessibility invariants, carried over from the portfolio because they are
right, not because they are inherited:

- `.prompt` spans stay `aria-hidden="true"` — screen readers announcing
  "erik at insyn tilde dollar" before every heading is unusable.
- One `h1` per page, no skipped levels.
- Sortable headers are `<th scope="col">` with a `<button>` inside and
  `aria-sort` on the `th`. Not a click handler on a bare `th`.
- Everything animated is skipped under `prefers-reduced-motion: reduce`.

### 16.5 What the frontend may and may not render

Two rules that fail **silently** — the page keeps working while it is wrong.

1. **No person index (invariant 15, §8.1).** `pdmr` and `position` render
   **only** inside `company/<LEI>.json`'s transaction list. No name search, no
   person page, no name column on the leaderboard, no sorting or filtering by
   person anywhere. This is why `companies.json` carries no person fields and
   why the global `Transaction` model omits them — do not reintroduce at the
   view layer what the schema deliberately withholds.

2. **`market_cap: null` renders `—`, never `0`** (invariant 11, §6.4). Same for
   `pct_of_mcap`. A `0` market cap reaching the table means the sentinel
   escaped `market_cap_current`. Mirror `export_static.py`'s guard as a
   `console.warn` client-side so it is visible if it ever happens rather than
   being read as a real company worth nothing.

Formatting, since money is a plain number by design (§6.3):

- `Intl.NumberFormat('sv-SE', { notation: 'compact' })` in table cells, with
  the full value in `title`. Never format server-side.
- Dates from FI are Europe/Stockholm local text — print them verbatim, do not
  parse and re-render through `Date`. Only `computed_at` / `last_ingest_at`
  carry an offset.
- Negative net is a sell. Colour it with `--err` and give it a sign; do not
  rely on colour alone.

### 16.6 Default sort

**`net_value_sek` descending**, on the 30d board, as the landing state.

§14.4's caveat stands and is worth a revisit once the backfill lands: net
descending will rank Ericsson, Investor and Volvo at the top every time — they
are simply larger. `pct_of_mcap` is the more informative ranking but is `null`
for roughly a quarter of companies and noisy for micro caps, and
`buyer_count` distinguishes "five directors bought" from "one director bought
five times". All four ship in every entry, so changing the default is one line.

### 16.7 Caching

`insyn.css` and `js/*.js` carry `?v=N`, bumped together on any change — the
same single shared number `malmgren.dev` uses, and for the same reason: Pages
serves assets with a long `max-age` and a stale script against fresh markup
fails in ways that look like bugs.

**The JSON ships with a plain `fetch('./data/…')` and no cache-busting.** Do
not build a `meta.json`-as-manifest scheme: it costs two serial round trips
before first paint on every load, and it may buy nothing. Whether it is needed
at all is one `curl` after the first deploy — see the checklist in
`deploy/README.md`. Add it only if that shows a long `cache-control` on
`.json`.

### 16.8 File layout

```
frontend/
  CNAME                 insyn.malmgren.dev — must be in the artifact
  index.html
  company.html
  about.html
  404.html
  css/insyn.css         copied tokens + app rules
  js/insyn.js           shared: fetch, format, DOM helpers, the null guards
  js/leaderboard.js
  js/company.js
  js/about.js
```

---

### Acceptance checks — frontend

- `site/` after the assemble step contains `index.html` **and**
  `data/meta.json`; `dist/` still contains `meta.json` at its root
  (`tests/test_api.py` and `doctor` unchanged and green).
- Every page loads with no console errors against a real `site/` served by
  `python3 -m http.server`.
- A company with `market_cap: null` renders `—` in both the leaderboard and its
  detail page. No `0` and no negative `pct_of_mcap` anywhere. (Grep the DOM:
  `document.body.innerText` must not contain a market-cap cell of `0`.)
- `grep -ri "pdmr" frontend/js/` matches **only** `company.js`.
- Sorting every column and switching all four periods produces no network
  request after the first fetch of each period.
- `company.html?lei=<a real LEI>` renders; `?lei=nonsense` shows a readable
  error, not a blank page.
- Keyboard: tab reaches every sort button, `aria-sort` updates, focus ring
  visible. Zoom to 200% — no horizontal page scroll outside the table's own
  container.
- Light mode (`prefers-color-scheme: light`) is a full inversion with no
  unreadable pair.
