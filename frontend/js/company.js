/* company.js — company.html.
 *
 * Two modes: `?lei=` renders one company, no query renders a picker over
 * companies.json so the nav link has somewhere to land.
 *
 * THIS IS THE ONLY FILE ON THE SITE THAT RENDERS `pdmr` OR `position`.
 * §8.1 grants person fields on a company's own transaction list and nowhere
 * else (invariant 15): no name search, no person page, no name column on the
 * leaderboard, no sorting or filtering by person anywhere. The picker below
 * filters on company name and ticker only — the same two fields
 * companies.json carries, which is not an accident.
 */

import { url, getJSON, fmt, el, clear, setStatus, moneyCell, dashCell, stamp, param }
    from './insyn.js?v=1';

/* agg_company_period is written per window only when counted transactions
 * exist there, and the export orders the array alphabetically — '365d', '90d',
 * 'all', 'ytd' — which reads as nonsense. Impose the chronological order and
 * say plainly that an absent window means nothing was counted in it. */
const PERIOD_ORDER = ['30d', '90d', '365d', 'ytd', 'all'];
const PERIOD_LABEL = {
    '30d': '30 days', '90d': '90 days', '365d': '365 days',
    ytd: 'year to date', all: 'all time',
};

const statusNode = document.getElementById('status');
const detailNode = document.getElementById('detail');
const pickerNode = document.getElementById('picker');
const headingNode = document.getElementById('heading');

function section(title, ...children) {
    return el('section', {}, el('h2', { class: 'section-head', text: title }), ...children);
}

function scroller(table) {
    return el('div', { class: 'scroller' }, table);
}

/* ---------- the picker ---------- */

async function renderPicker() {
    headingNode.textContent = 'ls companies/';
    document.title = 'companies — insyn.malmgren.dev';
    pickerNode.hidden = false;
    setStatus(statusNode, 'loading', 'Loading the company index…');

    let index;
    try {
        index = await getJSON(url.companies());
    } catch (err) {
        setStatus(statusNode, 'error', `Could not load the company index: ${err.message}`);
        return;
    }
    setStatus(statusNode, 'ready', '');

    const input = document.getElementById('company-filter');
    const list = document.getElementById('company-list');
    const count = document.getElementById('company-count');

    /* Every LEI FI has ever filed against, including the ones with no counted
     * transactions — 2049 against the leaderboard's 251. Duplicate names on
     * distinct LEIs are real and are shown as they are: FI's LEIs are messy
     * and folding them together is a data fix (data/seed/issuer_alias.csv),
     * not something to paper over in the view. */
    const draw = () => {
        const needle = input.value.trim().toLocaleLowerCase('sv');
        const rows = index.companies.filter((c) =>
            !needle
            || c.name.toLocaleLowerCase('sv').includes(needle)
            || (c.ticker || '').toLocaleLowerCase('sv').includes(needle));

        clear(list);
        const frag = document.createDocumentFragment();
        for (const c of rows) {
            frag.append(el('li', {},
                el('a', { href: `company.html?lei=${encodeURIComponent(c.lei)}`, text: c.name }),
                c.ticker ? el('span', { class: 'muted', text: ` ${c.ticker}` }) : null,
                el('span', { class: 'muted', text: ` ${c.lei}` })));
        }
        list.append(frag);
        count.textContent = needle
            ? `${fmt.int(rows.length)} of ${fmt.int(index.companies.length)} companies`
            : `${fmt.int(index.companies.length)} companies`;
    };

    input.addEventListener('input', draw);
    draw();
}

/* ---------- one company ---------- */

function facts(c) {
    const dl = el('dl', { class: 'kv' });
    const row = (term, ...value) => {
        dl.append(el('dt', { text: term }), el('dd', {}, ...value));
    };

    row('Name', c.name);
    row('Ticker', c.ticker || el('span', { class: 'muted', text: '—' }));
    row('LEI', el('code', { text: c.lei }));
    row('Primary ISIN', c.primary_isin
        ? el('code', { text: c.primary_isin })
        : el('span', { class: 'muted', text: '—' }));

    /* null renders '—', never 0 (invariant 11). Only ~20% of issuers have a
     * market cap, so this is the common case and has to look deliberate. */
    if (c.market_cap === 0) {
        console.warn(`insyn: market_cap sentinel 0 reached the view for ${c.lei} `
            + '— a query bypassed market_cap_current (invariant 11)');
    }
    if (c.market_cap === null || c.market_cap === undefined) {
        row('Market cap', el('span', { class: 'muted', text: '— not available' }));
    } else {
        row('Market cap',
            el('span', { title: `${fmt.full(c.market_cap)} SEK`, text: `${fmt.compact(c.market_cap)} SEK` }),
            c.market_cap_as_of
                ? el('span', { class: 'muted', text: ` as of ${c.market_cap_as_of}` })
                : null);
    }
    row('Verification', c.verification === 'ok'
        ? 'ok — values checked against the market cap'
        : el('span', { class: 'tag-unverifiable', text: 'unverifiable — no market cap for this issuer' }));
    return dl;
}

function variants(c) {
    if (!c.name_variants || c.name_variants.length < 2) return null;
    return section('cat name-variants.txt',
        el('p', { class: 'prose muted' },
            'FI files this issuer under more than one name. The LEI is what the '
            + 'aggregate keys on; the display name is the most frequent recent one.'),
        el('ul', { class: 'variants' }, c.name_variants.map((v) => el('li', {},
            el('strong', { text: v.name }),
            el('span', {
                class: 'muted',
                text: ` — ${fmt.int(v.n_rows)} rows, ${v.first_seen || '?'} to ${v.last_seen || '?'}`,
            })))));
}

function periods(c) {
    const present = PERIOD_ORDER
        .map((p) => c.periods.find((row) => row.period === p))
        .filter(Boolean);

    if (!present.length) {
        return section('cat periods.tsv',
            el('p', { class: 'prose muted' },
                'No counted transactions in any window. FI has filed rows against this '
                + 'issuer, but none of them are an acquisition or a disposal — see the '
                + 'counting rule on the about page.'));
    }

    const head = el('tr', {}, [
        el('th', { scope: 'col', text: 'period' }),
        el('th', { scope: 'col', text: 'window' }),
        el('th', { scope: 'col', class: 'num', text: 'net' }),
        el('th', { scope: 'col', class: 'num', text: 'bought' }),
        el('th', { scope: 'col', class: 'num', text: 'sold' }),
        el('th', { scope: 'col', class: 'num', text: 'tx' }),
        el('th', { scope: 'col', class: 'num', text: 'buyers' }),
        el('th', { scope: 'col', class: 'num', text: 'sellers' }),
    ]);

    const body = el('tbody', {}, present.map((p) => el('tr', {}, [
        el('th', { scope: 'row', text: PERIOD_LABEL[p.period] || p.period }),
        el('td', { class: 'muted', text: `${p.period_start} → ${p.period_end}` }),
        moneyCell(p.net_value_sek, { signed: true }),
        moneyCell(p.buy_value_sek),
        moneyCell(p.sell_value_sek),
        el('td', { class: 'num', text: fmt.int(p.tx_count) }),
        el('td', { class: 'num', text: fmt.int(p.buyer_count) }),
        el('td', { class: 'num', text: fmt.int(p.seller_count) }),
    ])));

    const missing = PERIOD_ORDER.filter((p) => !c.periods.some((row) => row.period === p));

    return section('cat periods.tsv',
        scroller(el('table', {}, el('thead', {}, head), body)),
        missing.length
            ? el('p', { class: 'prose muted' },
                `Not listed: ${missing.map((p) => PERIOD_LABEL[p] || p).join(', ')} — `
                + 'no counted transactions in those windows.')
            : null);
}

/* The transaction list. `pdmr` and `position` appear here and only here. */
function transactions(c) {
    const capped = c.tx_count_total > c.recent_transactions.length;
    const note = capped
        ? `Showing the ${fmt.int(c.recent_transactions.length)} most recent of `
          + `${fmt.int(c.tx_count_total)} filed rows.`
        : `All ${fmt.int(c.tx_count_total)} filed rows.`;

    const head = el('tr', {}, [
        el('th', { scope: 'col', text: 'date' }),
        el('th', { scope: 'col', class: 'opt', text: 'published' }),
        el('th', { scope: 'col', text: 'person' }),
        el('th', { scope: 'col', class: 'opt', text: 'position' }),
        el('th', { scope: 'col', text: 'nature' }),
        el('th', { scope: 'col', class: 'opt', text: 'instrument' }),
        el('th', { scope: 'col', class: 'num', text: 'volume' }),
        el('th', { scope: 'col', class: 'num', text: 'price' }),
        el('th', { scope: 'col', class: 'opt', text: 'ccy' }),
        el('th', { scope: 'col', class: 'num', text: 'value SEK' }),
        el('th', { scope: 'col', text: 'counted' }),
    ]);

    const rows = c.recent_transactions.map((t) => {
        const tr = el('tr', {}, [
            /* Verbatim — FI's dates are Europe/Stockholm local text and must
             * not be parsed and re-rendered through Date() (§16.5). */
            el('td', { text: t.transaction_date }),
            el('td', { class: 'opt muted', text: t.published_date }),
            el('td', { text: t.pdmr }),
            el('td', { class: 'opt muted', text: t.position }),
            el('td', { text: t.nature }),
            el('td', { class: 'opt', text: t.instrument_name || t.instrument_type }),
            t.volume === null ? dashCell() : el('td', { class: 'num', text: fmt.full(t.volume) }),
            t.price === null ? dashCell() : el('td', { class: 'num', text: fmt.price(t.price) }),
            el('td', { class: 'opt muted', text: t.currency }),
            /* sign is applied here so a sale reads as negative in the list the
             * same way it does in the totals; gross_value_sek is unsigned. */
            t.gross_value_sek === null || !t.is_counted
                ? dashCell()
                : moneyCell(t.gross_value_sek * (t.sign || 1), { signed: true }),
            /* No row is ever uncounted without a reason (invariant 8), so the
             * reason is shown rather than left as a silent blank. */
            el('td', {
                class: t.is_counted ? null : 'muted',
                text: t.is_counted ? 'yes' : (t.exclude_reason || 'no'),
            }),
        ]);
        tr.dataset.counted = String(t.is_counted);
        return tr;
    });

    return section('tail -n transactions.tsv',
        el('p', { class: 'tx-note prose', text: note }),
        scroller(el('table', {}, el('thead', {}, head), el('tbody', {}, rows))));
}

async function renderCompany(lei) {
    setStatus(statusNode, 'loading', `Loading ${lei}…`);
    let c;
    try {
        c = await getJSON(url.company(lei));
    } catch (err) {
        headingNode.textContent = `cat company/${lei}.json`;
        setStatus(statusNode, 'error',
            `No company with LEI ${lei} (${err.message}).`);
        detailNode.append(el('p', {},
            'Check the LEI, or ',
            el('a', { href: 'company.html', text: 'browse the company index' }),
            '.'));
        return;
    }

    setStatus(statusNode, 'ready', '');
    headingNode.textContent = `cat ${c.name}`;
    document.title = `${c.name} — insyn.malmgren.dev`;

    detailNode.append(
        facts(c),
        variants(c),
        periods(c),
        transactions(c),
        el('p', { class: 'prose muted', text: `Computed ${stamp(c.computed_at)}.` }));
}

const lei = param('lei');
if (lei) {
    renderCompany(lei);
} else {
    renderPicker();
}
