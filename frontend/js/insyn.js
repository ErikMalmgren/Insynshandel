/* insyn.js — the shared layer: URLs, fetching, formatting, DOM.
 *
 * Person names render inside company.js and nowhere else: a company's own
 * transaction list is where they belong, and there is no person page, no
 * name column on the leaderboard and no search across companies. (Searching
 * within one company's list is fine — FI's own search client finds filings
 * by person name, so that adds nothing the regulator does not.)
 *
 * The leaderboard does use two person-derived fields, neither of them a name:
 * a number per person that is only meaningful within one company (so it can
 * count distinct buyers and sellers, and cannot follow anyone from one
 * company to the next), and a coarse role group (VD, CFO, Styrelse…) to
 * filter on. Both come from the fact export; `pdmr` itself never leaves
 * company.js.
 */

/* ---------- where the data comes from ----------
 *
 * Static JSON next to the pages, under window.API_BASE (default './data',
 * which is where the ingest workflow's assemble step puts the export). The
 * file layout is mapped here, once, so every caller asks for a resource rather
 * than building a path.
 */

const API_BASE = (typeof window !== 'undefined' && typeof window.API_BASE === 'string'
    && window.API_BASE) || './data';

const q = encodeURIComponent;

export const url = {
    meta:        ()    => `${API_BASE}/meta.json`,
    dataQuality: ()    => `${API_BASE}/data-quality.json`,
    factsMeta:   ()    => `${API_BASE}/facts-meta.json`,
    /* keyed on the build, so the HTTP cache can never hand a fresh
     * facts-meta.json a year file from an older deploy — see leaderboard.js */
    facts:       (y, build) => `${API_BASE}/facts/${q(y)}.json?b=${q(build)}`,
    companies:   ()    => `${API_BASE}/companies.json`,
    company:     (lei) => `${API_BASE}/company/${q(lei)}.json`,
    companyTx:   (lei) => `${API_BASE}/company-tx/${q(lei)}.json`,
};

/* ---------- fetching ----------
 *
 * Cached per URL, forever. Once the years a view needs have arrived, sorting a
 * column, typing in the filter box and changing the query within those years
 * are pure client work. That is an acceptance check: sorting and re-selecting
 * a period must issue no request.
 *
 * No cache-busting for freshness. Do not add a meta.json-as-manifest scheme
 * without first reading the cache-control header off the deployed site — it
 * costs two serial round trips before first paint and may buy nothing. The
 * one exception is about correctness, not freshness: the leaderboard's year
 * files are numbers indexing into facts-meta.json, so the two must come from
 * the same deploy. facts-meta.json is revalidated on every page load, and the
 * year files are keyed on its build. That adds no round trip — the year files
 * wait on facts-meta.json anyway.
 */

const cache = new Map();

export function getJSON(target, init) {
    if (!cache.has(target)) {
        cache.set(target, fetch(target, init).then((r) => {
            if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${target}`);
            return r.json();
        }).catch((err) => {
            /* A rejected promise left in the map would be replayed on every
             * later call, so a transient failure must not be cached. */
            cache.delete(target);
            throw err;
        }));
    }
    return cache.get(target);
}

/* ---------- formatting ----------
 *
 * Money is a plain number in the JSON by design — the export never
 * formats it. Compact in the cell, full value in `title`.
 *
 * md (miljard, 10⁹) is the largest unit. sv-SE compact steps up to bn
 * (biljon, 10¹²) past 999,9 md, and a column mixing the two is a
 * factor-of-a-thousand misread waiting to happen — worse, English readers
 * take bn for 10⁹. So from 10⁹ up the number is written in md: 1 200 md.
 */

const MD = 1e9;
const nfCompact       = new Intl.NumberFormat('sv-SE', { notation: 'compact', maximumFractionDigits: 1 });
const nfCompactSigned = new Intl.NumberFormat('sv-SE', { notation: 'compact', maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const nfMd            = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 1 });
const nfMdSigned      = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const nfFull          = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 0 });
const nfPrice         = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 4 });
const nfInt           = new Intl.NumberFormat('sv-SE');
/* Fixed at two decimals so a column of percentages lines up on the comma. */
const nfPct           = new Intl.NumberFormat('sv-SE', { style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });

export const fmt = {
    int:     (n) => nfInt.format(n),
    full:    (n) => nfFull.format(n),
    price:   (n) => nfPrice.format(n),
    compact: (n) => (Math.abs(n) >= MD ? `${nfMd.format(n / MD)}\u00a0md` : nfCompact.format(n)),
    signed:  (n) => (Math.abs(n) >= MD ? `${nfMdSigned.format(n / MD)}\u00a0md` : nfCompactSigned.format(n)),
    pct:     (n) => nfPct.format(n),
};

/* ---------- DOM ---------- */

export function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of children.flat()) {
        if (c === null || c === undefined || c === false) continue;
        node.append(c);
    }
    return node;
}

export function clear(node) {
    node.replaceChildren();
}

/* Every page ships a .status paragraph so a failure is a sentence rather than
 * a blank screen. `ready` hides it via CSS. */
export function setStatus(node, state, message) {
    if (!node) return;
    node.dataset.state = state;
    node.textContent = message || '';
}

/* ---------- cells ----------
 *
 * The three that carry a rule. Everything else is a plain <td>.
 */

/* A money cell. Negative is a sell: coloured AND signed, never colour alone. */
export function moneyCell(value, { signed = false, className = '' } = {}) {
    if (value === null || value === undefined) return dashCell(className);
    const cls = ['num', className, signed && value < 0 ? 'neg' : ''].filter(Boolean).join(' ');
    const td = el('td', { class: cls, title: `${nfFull.format(value)} SEK` });
    td.textContent = signed ? fmt.signed(value) : fmt.compact(value);
    return td;
}

/* Market cap.
 *
 * `null` renders an em dash, never 0. A 0 reaching this
 * function means the sentinel escaped `market_cap_current` somewhere between
 * the view and the browser, so it is worth a console.warn even though the cell
 * renders harmlessly — read as a real company worth nothing, it is a silent
 * lie, and this is the client-side mirror of export_static.py's guard.
 */
export function mcapCell(value, lei, { className = '' } = {}) {
    if (value === 0) {
        console.warn(
            `insyn: market_cap sentinel 0 reached the view for ${lei} — `
            + 'a query bypassed market_cap_current');
    }
    if (value === null || value === undefined) return dashCell(className);
    return moneyCell(value, { className });
}

/* Net value as a share of market cap — a ratio in the JSON, rendered as a
 * percentage. Negative is normal: it is a company whose insiders were net
 * sellers, and the guard that once flagged it was wrong. **Zero is normal
 * too** — bought exactly as much as sold — so there is deliberately no
 * sentinel check here. The 0 that means something is a market cap of 0, which
 * is mcapCell's business; warning on a 0 ratio only trains people to ignore
 * the warning that matters.
 */
export function pctCell(value, { className = '' } = {}) {
    if (value === null || value === undefined) return dashCell(className);
    const cls = ['num', className, value < 0 ? 'neg' : ''].filter(Boolean).join(' ');
    return el('td', { class: cls, text: fmt.pct(value), title: `${value}` });
}

export function dashCell(className = '') {
    return el('td', {
        class: ['num', 'dash', className].filter(Boolean).join(' '),
        text: '—',
        title: 'not available',
    });
}

/* ---------- misc ---------- */

/* FI's dates are Europe/Stockholm local text. Print them verbatim — parsing
 * and re-rendering through Date() shifts them by a day near midnight for
 * anyone in another zone. Only computed_at / last_ingest_at carry an
 * offset, and those are the only ones worth localising. */
export function stamp(iso) {
    if (!iso) return 'unknown';
    return iso.replace('T', ' ').replace(/(\+|-)\d{2}:\d{2}$/, '');
}

export function param(name) {
    return new URLSearchParams(window.location.search).get(name);
}

/* Tickers render with the share class dash-separated (TELE2-B), but people
 * type them either way, so the dash and any space come out of both sides. */
export function tickerMatches(ticker, needle) {
    const bare = (s) => s.toLocaleLowerCase('sv').replace(/[\s-]/g, '');
    const n = bare(needle);
    return n !== '' && bare(ticker || '').includes(n);
}
