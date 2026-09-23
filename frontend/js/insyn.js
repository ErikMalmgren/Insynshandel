/* insyn.js — the shared layer: URLs, fetching, formatting, DOM.
 *
 * No person field — neither the reporting insider's name nor their position —
 * is read anywhere in this file. Those two render inside company.js and
 * nowhere else, because they belong on a company's own transaction list
 * only: no name search, no person page, no name column on the
 * leaderboard, no sorting or filtering by person anywhere. Anything generic
 * enough to live in a shared module is generic enough to end up on the
 * leaderboard by accident, which is how a person index gets built without
 * anyone ever deciding to build one.
 *
 * That makes a check you can run: the field name must not appear
 * outside company.js. It is satisfied structurally — the rendering lives
 * there — not by spelling the word differently here.
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
    leaderboard: (p)   => `${API_BASE}/leaderboard-${q(p)}.json`,
    companies:   ()    => `${API_BASE}/companies.json`,
    company:     (lei) => `${API_BASE}/company/${q(lei)}.json`,
};

/* ---------- fetching ----------
 *
 * Cached per URL, forever. Switching period, sorting a column and typing in
 * the filter box are pure client work — every sortable field ships in every
 * entry, so there is nothing to go back to the network for. That is an
 * acceptance check: sorting and re-selecting a period must issue no request.
 *
 * No cache-busting on the JSON. Do not add a meta.json-as-manifest
 * scheme without first reading the cache-control header off the deployed site
 * — it costs two serial round trips before first paint and may buy nothing.
 */

const cache = new Map();

export function getJSON(target) {
    if (!cache.has(target)) {
        cache.set(target, fetch(target).then((r) => {
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
 */

const nfCompact       = new Intl.NumberFormat('sv-SE', { notation: 'compact', maximumFractionDigits: 1 });
const nfCompactSigned = new Intl.NumberFormat('sv-SE', { notation: 'compact', maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const nfFull          = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 0 });
const nfPrice         = new Intl.NumberFormat('sv-SE', { maximumFractionDigits: 4 });
const nfInt           = new Intl.NumberFormat('sv-SE');
/* Fixed at two decimals so a column of percentages lines up on the comma. */
const nfPct           = new Intl.NumberFormat('sv-SE', { style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });

export const fmt = {
    int:     (n) => nfInt.format(n),
    full:    (n) => nfFull.format(n),
    price:   (n) => nfPrice.format(n),
    compact: (n) => nfCompact.format(n),
    signed:  (n) => nfCompactSigned.format(n),
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
