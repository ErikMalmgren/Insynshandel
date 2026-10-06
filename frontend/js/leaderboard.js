/* leaderboard.js — index.html.
 *
 * The board is aggregated here, in the browser, from the fact export:
 * facts-meta.json (companies, natures, instruments, role groups, the preset
 * windows) and facts/{year}.json (every row with a trustworthy SEK value, as
 * parallel arrays). A query is a date range plus a selection of natures,
 * instruments and role groups. The default selection — the counted natures,
 * every instrument and role — over a preset window reproduces
 * agg_company_period exactly; `insyn doctor` checks that against these same
 * files.
 *
 * Only the years a range touches are fetched, each once (getJSON caches), so
 * sorting, filtering and re-querying years already loaded issue no request.
 *
 * No person name is read here — see the header of insyn.js. A fact row
 * carries a per-company person number and a role-group bitmask, nothing more.
 */

import { url, getJSON, fmt, el, clear, setStatus, moneyCell, mcapCell, pctCell, tickerMatches }
    from './insyn.js?v=4';

const PRESETS = [
    ['7d',   '7 days'],
    ['30d',  '30 days'],
    ['90d',  '90 days'],
    ['365d', '365 days'],
    ['all',  'all time'],
];
const DEFAULT_PRESET = '30d';

/* Display order. `opt` comes off below 46rem: a phone shows
 * company · net · insiders · % mcap, which fits without scrolling. */
const COLUMNS = [
    { key: 'name',   label: 'company',  kind: 'company', cls: 'col-name' },
    { key: 'net',    label: 'net',      kind: 'money', signed: true },
    { key: 'buy',    label: 'bought',   kind: 'money', cls: 'opt' },
    { key: 'sell',   label: 'sold',     kind: 'money', cls: 'opt' },
    { key: 'buyers', label: 'insiders', kind: 'insiders',
      title: 'distinct insiders who bought / who sold — sorts by buyers' },
    { key: 'tx',     label: 'tx',       kind: 'int',   cls: 'opt' },
    { key: 'latest', label: 'latest',   kind: 'date',  cls: 'opt',
      title: 'the newest matching transaction' },
    { key: 'mcap',   label: 'mcap',     kind: 'mcap',  cls: 'opt' },
    { key: 'pct',    label: '% mcap',   kind: 'pct',
      title: 'net as a share of the current market cap' },
];

/* Net descending is the landing state. Ranking by net puts the largest
 * companies on top every time; % mcap is the more telling column but is empty
 * for most issuers. */
const DEFAULT_SORT = { key: 'net', dir: 'desc' };

/* "Shares only": FI's own Aktie, plus the blank type the older filings use —
 * config.EQUITY_INSTRUMENT_TYPES treats blank as equity too. */
const SHARES = new Set(['Aktie', '']);
const BLANK = '(blank)';

const state = {
    preset: DEFAULT_PRESET,     // null = a hand-picked range
    from: '',
    to: '',
    natures: new Set(),         // indices into meta.natures
    instruments: new Set(),     // indices into meta.instruments
    roles: new Set(),           // indices into meta.position_groups
    key: DEFAULT_SORT.key,
    dir: DEFAULT_SORT.dir,
    filter: '',
};

const statusNode = document.getElementById('status');
const captionNode = document.getElementById('board-caption');
const theadRow = document.getElementById('board-head');
const tbody = document.getElementById('board-body');
const filterInput = document.getElementById('filter');
const tabsNode = document.getElementById('tabs');
const fromInput = document.getElementById('from');
const toInput = document.getElementById('to');
const summaryNode = document.getElementById('query-summary');
const natureBox = document.getElementById('q-nature');
const instrumentBox = document.getElementById('q-instrument');
const roleBox = document.getElementById('q-role');

let meta = null;
let windows = new Map();
let entries = null;         // the aggregated board, before the name filter
let matchedRows = 0;

/* A query changed while an earlier fetch is still in flight must win — the
 * slow response arriving second must not repaint the table. */
let generation = 0;

/* ---------- dates ----------
 *
 * The facts carry a date as days since meta.epoch. FI's dates are
 * Stockholm-local text, so the arithmetic is done in UTC, where a day is
 * always 86 400 000 ms and a date never shifts with the reader's zone. */

const DAY = 86400000;
const utc = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10));
let epoch = 0;
const dayOf = (iso) => Math.round((utc(iso) - epoch) / DAY);
const isoOf = (d) => new Date(epoch + d * DAY).toISOString().slice(0, 10);

/* ---------- the query ---------- */

const range = (n) => new Set(Array.from({ length: n }, (_, k) => k));
const defaultNatures = () => new Set(
    meta.natures.flatMap((n, k) => (n.default ? [k] : [])));
const instrumentName = (i) => i.name || BLANK;
const sameSet = (a, b) => a.size === b.size && [...a].every((k) => b.has(k));

function pickPreset(p) {
    const w = windows.get(p);
    state.preset = p;
    state.from = w.start;
    state.to = w.end;
}

/* The whole query lives in the URL, so a view can be linked to and survives
 * "back" from a company page. Only what differs from the default is written.
 * A selection is the names, repeated (?n=Förvärv&n=Teckning) — names survive
 * a change to the seed's order where indices would not. An empty selection is
 * one empty value, which matches no name. */
function readURL() {
    const q = new URLSearchParams(window.location.search);
    const pick = (key, names, fallback) => {
        if (!q.has(key)) return fallback;
        const want = new Set(q.getAll(key));
        return new Set(names.flatMap((name, k) => (want.has(name) ? [k] : [])));
    };
    state.natures = pick('n', meta.natures.map((n) => n.name), defaultNatures());
    state.instruments = pick('i', meta.instruments.map(instrumentName),
        range(meta.instruments.length));
    state.roles = pick('r', meta.position_groups, range(meta.position_groups.length));

    const all = windows.get('all');
    const valid = (iso) => /^\d{4}-\d{2}-\d{2}$/.test(iso || '');
    if (valid(q.get('from')) || valid(q.get('to'))) {
        state.preset = null;
        state.from = valid(q.get('from')) ? q.get('from') : all.start;
        state.to = valid(q.get('to')) ? q.get('to') : all.end;
    } else {
        const p = q.get('p');
        pickPreset(PRESETS.some(([k]) => k === p) && windows.has(p) ? p : DEFAULT_PRESET);
    }

    if (COLUMNS.some((c) => c.key === q.get('s'))) state.key = q.get('s');
    if (q.get('d') === 'asc' || q.get('d') === 'desc') state.dir = q.get('d');
    state.filter = q.get('q') || '';
}

function writeURL() {
    const q = new URLSearchParams();
    if (state.preset === null) {
        q.set('from', state.from);
        q.set('to', state.to);
    } else if (state.preset !== DEFAULT_PRESET) {
        q.set('p', state.preset);
    }
    const put = (key, set, names, isDefault) => {
        if (isDefault) return;
        if (!set.size) q.append(key, '');
        for (const k of [...set].sort((a, b) => a - b)) q.append(key, names[k]);
    };
    put('n', state.natures, meta.natures.map((n) => n.name),
        sameSet(state.natures, defaultNatures()));
    put('i', state.instruments, meta.instruments.map(instrumentName),
        state.instruments.size === meta.instruments.length);
    put('r', state.roles, meta.position_groups,
        state.roles.size === meta.position_groups.length);
    if (state.key !== DEFAULT_SORT.key || state.dir !== DEFAULT_SORT.dir) {
        q.set('s', state.key);
        q.set('d', state.dir);
    }
    if (state.filter) q.set('q', state.filter);
    const search = q.toString();
    window.history.replaceState(null, '',
        search ? `?${search}` : window.location.pathname);
}

/* ---------- aggregation ---------- */

function aggregate(shards) {
    const lo = dayOf(state.from);
    const hi = dayOf(state.to);
    /* null = nature not selected; 0 = selected, but neither a buy nor a sell
     * (a pledge, a loan) — it counts in tx and latest only. */
    const direction = meta.natures.map((n, k) => (state.natures.has(k) ? n.direction : null));
    const instrumentOk = meta.instruments.map((_, k) => state.instruments.has(k));
    let roleMask = 0;
    for (const k of state.roles) roleMask |= 1 << k;

    const byCompany = new Map();
    let rows = 0;
    for (const f of shards) {
        const { d, c, p, n, i, r, v } = f;
        for (let k = 0; k < f.count; k += 1) {
            if (d[k] < lo || d[k] > hi) continue;
            const dir = direction[n[k]];
            /* every row has at least one role bit (Övrigt when nothing
             * matched), so selecting every group never drops one */
            if (dir === null || !instrumentOk[i[k]] || !(r[k] & roleMask)) continue;
            let a = byCompany.get(c[k]);
            if (!a) {
                a = { buy: 0, sell: 0, tx: 0, buyers: new Set(), sellers: new Set(), latest: d[k] };
                byCompany.set(c[k], a);
            }
            a.tx += 1;
            rows += 1;
            if (d[k] > a.latest) a.latest = d[k];
            if (dir > 0) {
                a.buy += v[k];
                if (p[k] >= 0) a.buyers.add(p[k]);
            } else if (dir < 0) {
                a.sell += v[k];
                if (p[k] >= 0) a.sellers.add(p[k]);
            }
        }
    }

    matchedRows = rows;
    return [...byCompany].map(([ci, a]) => {
        const co = meta.companies[ci];
        const net = a.buy - a.sell;
        return {
            lei: co.lei,
            name: co.short_name,
            fullName: co.name,
            ticker: co.ticker,
            net,
            buy: a.buy,
            sell: a.sell,
            tx: a.tx,
            buyers: a.buyers.size,
            sellers: a.sellers.size,
            latest: a.latest,
            mcap: co.market_cap,
            /* the export guards market_cap against the 0 sentinel; a 0 that
             * got through anyway must not divide (mcapCell warns about it) */
            pct: co.market_cap ? net / co.market_cap : null,
        };
    });
}

async function load() {
    const mine = ++generation;
    if (state.from > state.to) {
        entries = null;
        clear(tbody);
        captionNode.textContent = '';
        setStatus(statusNode, 'error', 'The from date is after the to date.');
        return;
    }
    const first = +state.from.slice(0, 4);
    const last = +state.to.slice(0, 4);
    const years = meta.years.filter((y) => y >= first && y <= last);
    setStatus(statusNode, 'loading', `Loading ${first === last ? first : `${first}–${last}`}…`);
    try {
        const shards = await Promise.all(years.map((y) => getJSON(url.facts(y, meta.build))));
        if (mine !== generation) return;   // a newer query won
        /* A year file from another deploy indexes other company and nature
         * lists — its totals would look right and belong to the wrong rows.
         * It happens when the site is redeployed under an open tab. */
        if (shards.some((f) => f.build !== meta.build)) {
            entries = null;
            clear(tbody);
            captionNode.textContent = '';
            setStatus(statusNode, 'error',
                'The data has been updated since this page was loaded — reload the page.');
            return;
        }
        entries = aggregate(shards);
        setStatus(statusNode, 'ready', '');
        render();
    } catch (err) {
        if (mine !== generation) return;
        entries = null;
        clear(tbody);
        captionNode.textContent = '';
        setStatus(statusNode, 'error', `Could not load the transactions: ${err.message}`);
    }
}

/* ---------- the table ---------- */

function buildHead() {
    for (const col of COLUMNS) {
        const button = el('button', { type: 'button', text: col.label, title: col.title || null });
        button.addEventListener('click', () => sortBy(col));
        const th = el('th', {
            scope: 'col',
            class: [col.cls, col.kind === 'company' ? '' : 'num'].filter(Boolean).join(' ') || null,
        }, button);
        th.dataset.key = col.key;
        theadRow.append(th);
    }
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
        state.dir = col.kind === 'company' ? 'asc' : 'desc';
    }
    markSort();
    writeURL();
    render();
}

/* Missing values sort last in BOTH directions. Roughly 80% of issuers have no
 * market cap, and flipping the direction on that column should reorder the
 * companies that have one — not bury them under hundreds of dashes. */
function compare(a, b) {
    const col = COLUMNS.find((c) => c.key === state.key);
    const av = a[state.key];
    const bv = b[state.key];
    const an = av === null || av === undefined;
    const bn = bv === null || bv === undefined;
    if (an && bn) return a.name.localeCompare(b.name, 'sv');
    if (an) return 1;
    if (bn) return -1;

    let r = col.kind === 'company' ? av.localeCompare(bv, 'sv') : av - bv;
    if (state.dir === 'desc') r = -r;
    /* The facts are unordered by company, so ties need a stable tiebreak of
     * their own or the row order wanders between renders. */
    return r || a.name.localeCompare(b.name, 'sv') || a.lei.localeCompare(b.lei);
}

function matches(entry, needle) {
    if (!needle) return true;
    return entry.fullName.toLocaleLowerCase('sv').includes(needle)
        || entry.name.toLocaleLowerCase('sv').includes(needle)
        || tickerMatches(entry.ticker, needle);
}

function cell(entry, col) {
    switch (col.kind) {
        case 'company':
            return el('td', { class: col.cls },
                el('a', {
                    href: `company.html?lei=${encodeURIComponent(entry.lei)}`,
                    text: entry.name,
                    title: entry.name === entry.fullName ? null : entry.fullName,
                }),
                entry.ticker ? el('span', { class: 'ticker', text: entry.ticker }) : null);
        case 'money':
            return moneyCell(entry[col.key], { signed: col.signed, className: col.cls });
        case 'mcap':
            return mcapCell(entry.mcap, entry.lei, { className: col.cls });
        case 'pct':
            return pctCell(entry.pct, { className: col.cls });
        case 'insiders':
            return el('td', {
                class: 'num',
                text: `${fmt.int(entry.buyers)}/${fmt.int(entry.sellers)}`,
                title: `${entry.buyers} distinct ${entry.buyers === 1 ? 'insider' : 'insiders'} `
                    + `bought, ${entry.sellers} sold`,
            });
        case 'date':
            return el('td', { class: ['num', col.cls].join(' '), text: isoOf(entry.latest) });
        default:
            return el('td', {
                class: ['num', col.cls].filter(Boolean).join(' '),
                text: fmt.int(entry[col.key]),
            });
    }
}

function render() {
    if (!entries) return;
    const needle = state.filter.toLocaleLowerCase('sv');
    const rows = entries.filter((e) => matches(e, needle)).sort(compare);

    clear(tbody);
    const frag = document.createDocumentFragment();
    for (const entry of rows) {
        frag.append(el('tr', {}, COLUMNS.map((col) => cell(entry, col))));
    }
    tbody.append(frag);

    const companies = needle
        ? `${fmt.int(rows.length)} of ${fmt.int(entries.length)} companies`
        : `${fmt.int(entries.length)} companies`;
    captionNode.textContent = `${companies} · ${fmt.int(matchedRows)} transactions · `
        + `${state.from} to ${state.to}`;

    if (!rows.length) {
        tbody.append(el('tr', {},
            el('td', {
                colspan: COLUMNS.length,
                class: 'muted',
                text: entries.length
                    ? 'No company matches that filter.'
                    : 'No transaction matches this query.',
            })));
    }
}

/* ---------- controls ---------- */

function buildTabs() {
    for (const [period, label] of PRESETS) {
        if (!windows.has(period)) continue;
        const b = el('button', { type: 'button', 'data-period': period, text: label });
        b.addEventListener('click', () => {
            pickPreset(period);
            onQuery();
        });
        tabsNode.append(b);
    }
    const all = windows.get('all');
    for (const input of [fromInput, toInput]) {
        input.min = all.start;
        input.max = all.end;
        input.addEventListener('change', onRange);
    }
}

/* A hand-typed range that happens to equal a preset lights that tab up. An
 * emptied input means "from the start" / "up to today". */
function onRange() {
    const all = windows.get('all');
    state.from = fromInput.value || all.start;
    state.to = toInput.value || all.end;
    state.preset = PRESETS.map(([k]) => k).find((k) => windows.has(k)
        && windows.get(k).start === state.from && windows.get(k).end === state.to) || null;
    onQuery();
}

/* One checkbox; `key` names the state set it toggles, looked up on every
 * change because reset replaces the set. */
function checkbox(key, k, ...label) {
    const id = `q-${key}-${k}`;
    const box = el('input', { type: 'checkbox', id, 'data-set': key, 'data-k': k });
    box.addEventListener('change', () => {
        if (box.checked) state[key].add(k);
        else state[key].delete(k);
        onQuery();
    });
    return el('div', { class: 'q-item' }, box, el('label', { for: id }, ...label));
}

/* Bracketed shortcuts above a list: [all] [none] … */
function shortcuts(pairs) {
    return el('p', { class: 'q-shortcuts' }, pairs.map(([text, fn]) => {
        const b = el('button', { type: 'button', class: 'link-button', text });
        b.addEventListener('click', () => {
            fn();
            onQuery();
        });
        return b;
    }));
}

const DIRECTION = {
    1: ['+', 'counts as bought'],
    '-1': ['−', 'counts as sold'],
    0: ['·', 'counts in tx only — neither bought nor sold'],
};

function buildQuery() {
    /* natures, grouped by nature_map.category in the seed's order; a
     * category's name toggles the whole category */
    natureBox.append(shortcuts([
        ['default', () => { state.natures = defaultNatures(); }],
        ['all', () => { state.natures = range(meta.natures.length); }],
        ['none', () => { state.natures = new Set(); }],
    ]));
    const categories = new Map();
    meta.natures.forEach((n, k) => {
        if (!categories.has(n.category)) categories.set(n.category, []);
        categories.get(n.category).push(k);
    });
    const list = el('div', { class: 'q-natures' });
    for (const [category, ks] of categories) {
        const toggle = el('button', {
            type: 'button', class: 'link-button q-cat',
            text: category.replace(/_/g, ' ') || 'other',
            title: 'select or clear this whole group',
        });
        toggle.addEventListener('click', () => {
            const on = !ks.every((k) => state.natures.has(k));
            for (const k of ks) {
                if (on) state.natures.add(k);
                else state.natures.delete(k);
            }
            onQuery();
        });
        list.append(el('div', { class: 'q-group' }, toggle, ks.map((k) => {
            const [mark, meaning] = DIRECTION[meta.natures[k].direction];
            return checkbox('natures', k, meta.natures[k].name, ' ',
                el('span', { class: 'muted', title: meaning, text: mark }));
        })));
    }
    natureBox.append(list,
        el('p', { class: 'muted q-note' },
            '+ counts as bought, − as sold, · in tx only. Förvärv and Avyttring are the '
            + 'default; most of the rest are paper moves rather than trades — see the about page.'));

    instrumentBox.append(shortcuts([
        ['all', () => { state.instruments = range(meta.instruments.length); }],
        ['shares only', () => {
            state.instruments = new Set(meta.instruments.flatMap(
                (ins, k) => (SHARES.has(ins.name) ? [k] : [])));
        }],
        ['none', () => { state.instruments = new Set(); }],
    ]), el('div', { class: 'q-columns' },
        meta.instruments.map((ins, k) => checkbox('instruments', k, instrumentName(ins)))));

    roleBox.append(shortcuts([
        ['all', () => { state.roles = range(meta.position_groups.length); }],
        ['none', () => { state.roles = new Set(); }],
    ]), ...meta.position_groups.map((name, k) => checkbox('roles', k, name)),
    el('p', { class: 'muted q-note' },
        'From the filing\'s Befattning, grouped. A filing listing two roles '
        + '(VD and Styrelseledamot) is in both. Övrigt is everything that fits no group.'));

    document.getElementById('q-reset').addEventListener('click', () => {
        state.natures = defaultNatures();
        state.instruments = range(meta.instruments.length);
        state.roles = range(meta.position_groups.length);
        onQuery();
    });
}

function describe(set, names, noun) {
    if (set.size === names.length) return `all ${noun}`;
    if (!set.size) return `no ${noun}`;
    const picked = [...set].sort((a, b) => a - b).map((k) => names[k]);
    return picked.length <= 2 ? picked.join(', ') : `${picked.length} of ${names.length} ${noun}`;
}

function syncControls() {
    for (const b of tabsNode.children) {
        b.setAttribute('aria-pressed', String(b.dataset.period === state.preset));
    }
    fromInput.value = state.from;
    toInput.value = state.to;
    filterInput.value = state.filter;
    for (const box of document.querySelectorAll('input[data-set]')) {
        box.checked = state[box.dataset.set].has(+box.dataset.k);
    }
    const natures = [...state.natures].sort((a, b) => a - b).map((k) => meta.natures[k].name);
    summaryNode.textContent = [
        !natures.length ? 'no natures'
            : natures.length <= 3 ? natures.join(', ')
                : `${natures.slice(0, 2).join(', ')} +${natures.length - 2} more`,
        describe(state.instruments, meta.instruments.map(instrumentName), 'instruments'),
        describe(state.roles, meta.position_groups, 'positions'),
    ].join(' · ');
    markSort();
}

function onQuery() {
    syncControls();
    writeURL();
    load();
}

async function init() {
    buildHead();
    filterInput.addEventListener('input', () => {
        state.filter = filterInput.value.trim();
        writeURL();
        render();
    });
    try {
        /* revalidated, never taken from the HTTP cache unchecked: it decides
         * which build of the year files is fetched */
        meta = await getJSON(url.factsMeta(), { cache: 'no-cache' });
    } catch (err) {
        setStatus(statusNode, 'error', `Could not load the leaderboard: ${err.message}`);
        return;
    }
    epoch = utc(meta.epoch);
    windows = new Map(meta.windows.map((w) => [w.period, w]));
    readURL();
    buildTabs();
    buildQuery();
    syncControls();
    load();
}

init();
