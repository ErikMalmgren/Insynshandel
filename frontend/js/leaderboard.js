/* leaderboard.js — index.html.
 *
 * One fetch per period, cached for the life of the page. Every sortable field
 * ships in every entry, so switching period, sorting a column and typing
 * in the filter box are pure client work with no round trip.
 *
 * No person field is read here, by design — see the header of insyn.js.
 */

import { url, getJSON, fmt, el, clear, setStatus, moneyCell, mcapCell, pctCell }
    from './insyn.js?v=1';

const PERIODS = [
    ['30d',  '30 days'],
    ['90d',  '90 days'],
    ['365d', '365 days'],
    ['all',  'all time'],
];

/* The eleven columns, in display order.
 *
 * `opt` comes off below 46rem; the rest still scrolls inside .scroller. The
 * four dropped are the ones net and tx already summarise — a phone shows
 * name · ticker · net · tx · market cap · % of mcap · verification.
 */
const COLUMNS = [
    { key: 'name',           label: 'name',     kind: 'text',  cls: 'col-name' },
    { key: 'ticker',         label: 'ticker',   kind: 'text' },
    { key: 'net_value_sek',  label: 'net',      kind: 'money', signed: true },
    { key: 'buy_value_sek',  label: 'bought',   kind: 'money', cls: 'opt' },
    { key: 'sell_value_sek', label: 'sold',     kind: 'money', cls: 'opt' },
    { key: 'tx_count',       label: 'tx',       kind: 'int' },
    { key: 'buyer_count',    label: 'buyers',   kind: 'int',   cls: 'opt' },
    { key: 'seller_count',   label: 'sellers',  kind: 'int',   cls: 'opt' },
    { key: 'market_cap',     label: 'mcap',     kind: 'mcap' },
    { key: 'pct_of_mcap',    label: '% of mcap', kind: 'pct' },
    { key: 'verification',   label: 'verified', kind: 'text' },
];

/* Net descending on the 30d board is the landing state. All four of
 * net / pct_of_mcap / buyer_count ship in every entry, so changing this is one
 * line. Ranking by net puts the largest companies on top every time. */
const state = { period: '30d', key: 'net_value_sek', dir: 'desc', filter: '' };

const statusNode = document.getElementById('status');
const captionNode = document.getElementById('board-caption');
const theadRow = document.getElementById('board-head');
const tbody = document.getElementById('board-body');
const filterInput = document.getElementById('filter');
const tabsNode = document.getElementById('tabs');

/* A period selected while an earlier fetch is still in flight must win — the
 * slow response arriving second must not repaint the table. */
let generation = 0;

function buildTabs() {
    for (const [period, label] of PERIODS) {
        const b = el('button', {
            type: 'button',
            'data-period': period,
            'aria-pressed': String(period === state.period),
            text: label,
        });
        b.addEventListener('click', () => select(period));
        tabsNode.append(b);
    }
}

function buildHead() {
    for (const col of COLUMNS) {
        const button = el('button', { type: 'button', text: col.label });
        button.addEventListener('click', () => sortBy(col));
        const th = el('th', {
            scope: 'col',
            class: [col.cls, col.kind === 'text' ? '' : 'num'].filter(Boolean).join(' ') || null,
        }, button);
        th.dataset.key = col.key;
        theadRow.append(th);
    }
    markSort();
}

function markSort() {
    for (const th of theadRow.children) {
        th.setAttribute(
            'aria-sort',
            th.dataset.key === state.key
                ? (state.dir === 'asc' ? 'ascending' : 'descending')
                : 'none');
    }
}

function sortBy(col) {
    if (state.key === col.key) {
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    } else {
        state.key = col.key;
        /* Text reads best A→Z; a number you are ranking by reads best largest
         * first, which is what you wanted when you clicked it. */
        state.dir = col.kind === 'text' ? 'asc' : 'desc';
    }
    markSort();
    render();
}

function select(period) {
    state.period = period;
    for (const b of tabsNode.children) {
        b.setAttribute('aria-pressed', String(b.dataset.period === period));
    }
    load();
}

/* Missing values sort last in BOTH directions. Roughly 80% of issuers have no
 * market cap, and flipping the direction on that column should reorder the
 * companies that have one — not bury them under 200 dashes. */
function compare(a, b) {
    const col = COLUMNS.find((c) => c.key === state.key);
    const av = a[state.key];
    const bv = b[state.key];
    const an = av === null || av === undefined;
    const bn = bv === null || bv === undefined;
    if (an && bn) return a.name.localeCompare(b.name, 'sv');
    if (an) return 1;
    if (bn) return -1;

    let r = col.kind === 'text' ? String(av).localeCompare(String(bv), 'sv') : av - bv;
    if (state.dir === 'desc') r = -r;
    /* The export is unsorted, so ties need a stable tiebreak of their
     * own or the row order wanders between renders. */
    return r || a.name.localeCompare(b.name, 'sv') || a.lei.localeCompare(b.lei);
}

function matches(entry, needle) {
    if (!needle) return true;
    const name = entry.name.toLocaleLowerCase('sv');
    const ticker = (entry.ticker || '').toLocaleLowerCase('sv');
    return name.includes(needle) || ticker.includes(needle);
}

function cell(entry, col) {
    switch (col.kind) {
        case 'money':
            return moneyCell(entry[col.key], { signed: col.signed, className: col.cls });
        case 'mcap':
            return mcapCell(entry.market_cap, entry.lei, { className: col.cls });
        case 'pct':
            return pctCell(entry.pct_of_mcap, { className: col.cls });
        case 'int':
            return el('td', {
                class: ['num', col.cls].filter(Boolean).join(' '),
                text: fmt.int(entry[col.key]),
            });
        default:
            break;
    }
    if (col.key === 'name') {
        return el('td', { class: col.cls },
            el('a', { href: `company.html?lei=${encodeURIComponent(entry.lei)}`, text: entry.name }));
    }
    const value = entry[col.key];
    /* `verification` is derived: reads.py sets it from whether a market cap
     * exists at all, so this column is exactly `mcap !== '—'` and 'ok' means
     * "there was something to check against", not "checked and passed". The
     * per-row outlier verdict never reaches company level. Say so in the title rather
     * than letting a bare 'ok' claim more than it does. */
    return el('td', {
        class: [col.cls, value ? '' : 'dash',
                col.key === 'verification' && value === 'unverifiable' ? 'tag-unverifiable' : '',
               ].filter(Boolean).join(' ') || null,
        text: value || '—',
        title: col.key !== 'verification' ? null
            : value === 'ok'
                ? 'a market cap was on file to check these values against'
                : 'no market cap for this issuer — the values are counted, just unchecked',
    });
}

let entries = [];
let board = null;

function render() {
    if (!board) return;
    const needle = state.filter.toLocaleLowerCase('sv');
    const rows = entries.filter((e) => matches(e, needle)).sort(compare);

    clear(tbody);
    const frag = document.createDocumentFragment();
    for (const entry of rows) {
        frag.append(el('tr', {}, COLUMNS.map((col) => cell(entry, col))));
    }
    tbody.append(frag);

    const shown = needle
        ? `${fmt.int(rows.length)} of ${fmt.int(entries.length)} companies`
        : `${fmt.int(entries.length)} companies`;
    captionNode.textContent =
        `${shown} · transaction dates ${board.period_start} to ${board.period_end}`;

    if (!rows.length) {
        tbody.append(el('tr', {},
            el('td', { colspan: COLUMNS.length, class: 'muted', text: 'No company matches that filter.' })));
    }
}

async function load() {
    const mine = ++generation;
    setStatus(statusNode, 'loading', `Loading the ${state.period} board…`);
    try {
        const data = await getJSON(url.leaderboard(state.period));
        if (mine !== generation) return;   // a newer period won
        board = data;
        entries = data.entries;
        setStatus(statusNode, 'ready', '');
        render();
    } catch (err) {
        if (mine !== generation) return;
        board = null;
        clear(tbody);
        captionNode.textContent = '';
        setStatus(statusNode, 'error', `Could not load the ${state.period} board: ${err.message}`);
    }
}

buildTabs();
buildHead();
filterInput.addEventListener('input', () => {
    state.filter = filterInput.value.trim();
    render();
});
load();
