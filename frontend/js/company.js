/* company.js — company.html.
 *
 * Two modes: `?lei=` renders one company, no query renders a picker over
 * companies.json so the nav link has somewhere to land.
 *
 * THIS IS THE ONLY FILE ON THE SITE THAT RENDERS `pdmr` OR `position`.
 * Person fields belong on a company's own transaction list and nowhere
 * else: no person page, no name column on the leaderboard, no search across
 * companies. Within one company's list they are searchable — FI's own
 * search client already finds a filing by the reporting person's name, so
 * that makes nothing more findable than the regulator does. The picker below
 * filters on company name and ticker only — the same two fields
 * companies.json carries, which is not an accident.
 */

import { url, getJSON, fmt, el, clear, setStatus, moneyCell, dashCell, stamp, param,
    tickerMatches } from './insyn.js?v=3';

/* agg_company_period is written per window only when counted transactions
 * exist there, and the export orders the array alphabetically — '365d', '90d',
 * 'all', 'ytd' — which reads as nonsense. Impose the chronological order and
 * say plainly that an absent window means nothing was counted in it. */
const PERIOD_ORDER = ['30d', '90d', '365d', 'ytd', 'all'];
const PERIOD_LABEL = {
    '30d': '30 days', '90d': '90 days', '365d': '365 days',
    ytd: 'year to date', all: 'all time',
};

const TX_COLUMNS = 11;

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

/* Back goes where the reader came from when that was this site — the board,
 * the picker. From anywhere else (a new tab, a shared link) history.back()
 * would leave the site or do nothing, so the link's own href takes over. */
function wireBack(fallback) {
    const a = document.getElementById('back');
    if (!a) return;
    a.href = fallback;
    a.addEventListener('click', (ev) => {
        let fromHere = false;
        try {
            fromHere = new URL(document.referrer).origin === window.location.origin;
        } catch {
            /* no referrer */
        }
        if (fromHere && window.history.length > 1) {
            ev.preventDefault();
            window.history.back();
        }
    });
}

/* ---------- the picker ---------- */

async function renderPicker() {
    headingNode.textContent = 'companies';
    document.title = 'companies — insyn.malmgren.dev';
    wireBack('/');
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
            || tickerMatches(c.ticker, needle));

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

    /* null renders '—', never 0. Only ~20% of issuers have a
     * market cap, so this is the common case and has to look deliberate. */
    if (c.market_cap === 0) {
        console.warn(`insyn: market_cap sentinel 0 reached the view for ${c.lei} `
            + '— a query bypassed market_cap_current');
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

/* Each name is a toggle that narrows the transaction list to the rows FI
 * filed under it (`issuer_name`). */
function variants(c, pick) {
    if (!c.name_variants || c.name_variants.length < 2) return null;
    return section('name variants',
        el('p', { class: 'prose muted' },
            'FI files this issuer under more than one name. The LEI is what the '
            + 'aggregate keys on; the display name is the most frequent recent one. '
            + 'Pick a name to list only the transactions filed under it.'),
        el('ul', { class: 'variants' }, c.name_variants.map((v) => {
            const b = el('button', {
                type: 'button', class: 'link-button', 'aria-pressed': 'false',
                'data-variant': v.name, text: v.name,
            });
            b.addEventListener('click', () => pick(v.name));
            return el('li', {}, b, el('span', {
                class: 'muted',
                text: ` — ${fmt.int(v.n_rows)} rows, ${v.first_seen || '?'} to ${v.last_seen || '?'}`,
            }));
        })));
}

function periods(c) {
    const present = PERIOD_ORDER
        .map((p) => c.periods.find((row) => row.period === p))
        .filter(Boolean);

    if (!present.length) {
        return section('periods',
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

    return section('periods',
        scroller(el('table', {}, el('thead', {}, head), body)),
        missing.length
            ? el('p', { class: 'prose muted' },
                `Not listed: ${missing.map((p) => PERIOD_LABEL[p] || p).join(', ')} — `
                + 'no counted transactions in those windows.')
            : null);
}

/* ---------- the transaction list ----------
 *
 * `pdmr` and `position` appear here and only here.
 */

const instrumentOf = (t) => t.instrument_name || t.instrument_type;

/* One filed row. */
function txRow(t) {
    const tr = el('tr', {}, [
        /* Verbatim — FI's dates are Europe/Stockholm local text and must
         * not be parsed and re-rendered through Date(). */
        el('td', { class: 'tx-date', text: t.transaction_date }),
        el('td', { class: 'opt muted', text: t.published_date }),
        el('td', { text: t.pdmr }),
        el('td', { class: 'opt muted', text: t.position }),
        el('td', { text: t.nature }),
        el('td', { class: 'opt', text: instrumentOf(t) }),
        t.volume === null ? dashCell() : el('td', { class: 'num', text: fmt.full(t.volume) }),
        t.price === null ? dashCell() : el('td', { class: 'num', text: fmt.price(t.price) }),
        el('td', { class: 'opt muted', text: t.currency }),
        /* sign is applied here so a sale reads as negative in the list the
         * same way it does in the totals; gross_value_sek is unsigned. */
        t.gross_value_sek === null || !t.is_counted
            ? dashCell()
            : moneyCell(t.gross_value_sek * (t.sign || 1), { signed: true }),
        /* No row is ever uncounted without a reason, so the
         * reason is shown rather than left as a silent blank. */
        el('td', {
            class: t.is_counted ? null : 'muted',
            text: t.is_counted ? 'yes' : (t.exclude_reason || 'no'),
        }),
    ]);
    tr.dataset.counted = String(t.is_counted);
    return tr;
}

/* Consecutive rows by the same person fold into one run. ADJACENT ONLY, by
 * design: the list stays in date order and a run is a stretch of it — never a
 * per-person bucket gathered from across the list. Runs are cut from whatever
 * the filters left visible, so two stretches split by a hidden row join up. */
function groupRuns(rows) {
    const runs = [];
    for (const t of rows) {
        const last = runs[runs.length - 1];
        if (last && last[0].pdmr === t.pdmr) last.push(t);
        else runs.push([t]);
    }
    return runs;
}

/* The value every row of a run shares, or undefined when they differ. */
function shared(run, get) {
    const v = get(run[0]);
    return run.every((t) => get(t) === v) ? v : undefined;
}

const span = (a, b) => (a === b ? a : `${a} → ${b}`);
const mixedCell = (cls) => el('td', { class: [cls, 'muted'].filter(Boolean).join(' '), text: 'mixed' });

/* A run's summary row; `toggle` is its date cell's content. */
function runRow(run, toggle) {
    const newest = run[0];
    const published = run.map((t) => t.published_date).sort();
    const nature = shared(run, (t) => t.nature);
    const instrument = shared(run, instrumentOf);
    const currency = shared(run, (t) => t.currency);
    const position = shared(run, (t) => t.position);

    /* A volume only adds up across one kind of trade in one instrument, and a
     * price only averages across one currency on top of that. */
    const sameTrade = nature !== undefined && instrument !== undefined;
    const sized = run.every((t) => t.volume !== null);
    const volume = sameTrade && sized ? run.reduce((s, t) => s + t.volume, 0) : null;
    const priced = sameTrade && currency !== undefined && sized && volume > 0
        && run.every((t) => t.price !== null);
    const price = priced ? run.reduce((s, t) => s + t.volume * t.price, 0) / volume : null;

    const valued = run.filter((t) => t.is_counted && t.gross_value_sek !== null);
    const value = valued.reduce((s, t) => s + t.gross_value_sek * (t.sign || 1), 0);
    const counted = run.filter((t) => t.is_counted).length;

    const tr = el('tr', { class: 'tx-run' }, [
        el('td', { class: 'tx-date' },
            toggle,
            el('span', { class: 'muted', text: ` ${fmt.int(run.length)} rows` })),
        el('td', { class: 'opt muted', text: span(published[0], published[published.length - 1]) }),
        el('td', { text: newest.pdmr }),
        position === undefined ? mixedCell('opt') : el('td', { class: 'opt muted', text: position }),
        nature === undefined ? mixedCell() : el('td', { text: nature }),
        instrument === undefined ? mixedCell('opt') : el('td', { class: 'opt', text: instrument }),
        volume === null ? dashCell() : el('td', { class: 'num', text: fmt.full(volume) }),
        price === null
            ? dashCell()
            : el('td', { class: 'num', title: 'volume-weighted average', text: fmt.price(price) }),
        currency === undefined ? mixedCell('opt') : el('td', { class: 'opt muted', text: currency }),
        valued.length ? moneyCell(value, { signed: true }) : dashCell(),
        el('td', {
            class: counted ? null : 'muted',
            text: counted === run.length ? 'yes'
                : counted ? `${counted} of ${run.length}`
                    : (shared(run, (t) => t.exclude_reason) || 'no'),
        }),
    ]);
    tr.dataset.counted = counted ? '1' : '0';
    return tr;
}

/* A run of one is a plain row. A longer run is its own <tbody>: a summary row
 * whose date is the toggle, then the filed rows, hidden until opened. */
function runBody(run) {
    if (run.length === 1) return el('tbody', {}, txRow(run[0]));

    /* The list is newest first, so the run's last row is its oldest. */
    const mark = el('span', { class: 'tx-caret', 'aria-hidden': 'true', text: '▸' });
    const toggle = el('button', { type: 'button', class: 'tx-toggle', 'aria-expanded': 'false' },
        mark, span(run[run.length - 1].transaction_date, run[0].transaction_date));

    const children = run.map((t) => {
        const tr = txRow(t);
        tr.classList.add('tx-child');
        tr.hidden = true;
        return tr;
    });
    toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        toggle.setAttribute('aria-expanded', String(open));
        mark.textContent = open ? '▾' : '▸';
        for (const tr of children) tr.hidden = !open;
    });
    return el('tbody', { class: 'tx-group' }, runRow(run, toggle), children);
}

function transactionList(c) {
    const recent = c.recent_transactions;
    /* The capped list is an exact prefix of company-tx/{lei}.json (doctor
     * checks it), so it answers any view that needs no row past its end. */
    const capped = c.tx_count_total > recent.length;
    const limit = recent.length;
    const oldestRecent = recent.length ? recent[recent.length - 1].transaction_date : '';
    const windows = new Map((c.windows || []).map((w) => [w.period, w]));

    const state = { preset: 'all', from: '', to: '', variant: null, query: '', expanded: false };
    let full = capped ? null : recent;
    /* A view chosen while the full history is still loading must win — the
     * slow response arriving second must not repaint the list. */
    let generation = 0;

    /* ----- controls ----- */

    const tabs = el('div', { class: 'tabs', role: 'group', 'aria-label': 'Period' });
    for (const p of PERIOD_ORDER) {
        if (!windows.has(p)) continue;
        const b = el('button', { type: 'button', 'data-period': p, text: PERIOD_LABEL[p] });
        b.addEventListener('click', () => pickPeriod(p));
        tabs.append(b);
    }

    const fromInput = el('input', { type: 'date', id: 'tx-from', name: 'from' });
    const toInput = el('input', { type: 'date', id: 'tx-to', name: 'to' });
    const search = el('input', {
        type: 'search', id: 'tx-search', name: 'q', autocomplete: 'off', spellcheck: 'false',
        placeholder: 'person, nature, ISIN…',
    });

    const clearVariant = el('button', { type: 'button', class: 'link-button', text: 'clear' });
    clearVariant.addEventListener('click', () => pickVariant(null));
    const variantName = el('strong');
    const variantLine = el('p', { class: 'tx-filter', hidden: true },
        'filed as ', variantName, ' ', clearVariant);

    const note = el('p', { class: 'tx-note prose' });
    const status = el('p', { class: 'status', role: 'status', 'data-state': 'ready' });

    const head = el('thead', {}, el('tr', {}, [
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
    ]));
    const table = el('table', {}, head);

    const more = el('button', { type: 'button', class: 'link-button show-all', hidden: true });
    more.addEventListener('click', () => {
        state.expanded = !state.expanded;
        render();
    });

    const node = section('transactions',
        el('div', { class: 'controls' },
            tabs,
            el('div', { class: 'filter' },
                el('label', { for: 'tx-from', text: 'from' }), ' ', fromInput),
            el('div', { class: 'filter' },
                el('label', { for: 'tx-to', text: 'to' }), ' ', toInput),
            el('div', { class: 'filter' },
                el('label', { for: 'tx-search', text: 'search' }), ' ', search)),
        variantLine,
        note,
        status,
        scroller(table),
        el('p', {}, more));

    /* ----- state changes ----- */

    function syncTabs() {
        for (const b of tabs.children) {
            b.setAttribute('aria-pressed', String(b.dataset.period === state.preset));
        }
    }

    /* "All time" is no bounds at all rather than the window's: a row whose date
     * falls outside FI_EARLIEST..today is still a filed row, and the default
     * view must not hide it silently. */
    function pickPeriod(p) {
        const w = windows.get(p);
        state.preset = p;
        state.from = p === 'all' ? '' : w.start;
        state.to = p === 'all' ? '' : w.end;
        fromInput.value = state.from;
        toInput.value = state.to;
        syncTabs();
        render();
    }

    /* A hand-typed range that happens to equal a window lights that tab up. */
    function onRange() {
        state.from = fromInput.value;
        state.to = toInput.value;
        state.preset = !state.from && !state.to ? 'all'
            : PERIOD_ORDER.find((p) => p !== 'all' && windows.has(p)
                && windows.get(p).start === state.from && windows.get(p).end === state.to)
              || null;
        syncTabs();
        render();
    }

    function pickVariant(name) {
        state.variant = name === null || name === state.variant ? null : name;
        variantLine.hidden = state.variant === null;
        variantName.textContent = state.variant || '';
        for (const b of document.querySelectorAll('button[data-variant]')) {
            b.setAttribute('aria-pressed', String(b.dataset.variant === state.variant));
        }
        render();
        if (state.variant !== null) node.scrollIntoView({ block: 'start' });
    }

    fromInput.addEventListener('change', onRange);
    toInput.addEventListener('change', onRange);
    search.addEventListener('input', () => {
        state.query = search.value.trim();
        render();
    });

    /* ----- filtering ----- */

    /* Every field a reader might look a filing up by, person included —
     * lowercased once per row. */
    const haystacks = new WeakMap();
    function haystack(t) {
        let h = haystacks.get(t);
        if (h === undefined) {
            h = [t.transaction_date, t.published_date, t.pdmr, t.position, t.nature,
                t.instrument_name, t.instrument_type, t.isin, t.currency,
                t.exclude_reason, t.issuer_name]
                .filter(Boolean).join('\n').toLocaleLowerCase('sv');
            haystacks.set(t, h);
        }
        return h;
    }

    /* Dates are ISO text, so plain string comparison orders them; both
     * bounds are inclusive, the way the aggregate's BETWEEN is. */
    function matcher() {
        const terms = state.query.toLocaleLowerCase('sv').split(/\s+/).filter(Boolean);
        return (t) => {
            if (state.from && t.transaction_date < state.from) return false;
            if (state.to && t.transaction_date > state.to) return false;
            if (state.variant !== null && t.issuer_name !== state.variant) return false;
            return terms.every((term) => haystack(t).includes(term));
        };
    }

    const untouched = () => !state.from && !state.to && state.variant === null
        && !state.query && !state.expanded;
    /* The prefix holds every row on or after `from` once its oldest row is
     * older than `from`. Only the lower bound decides that; the other filters
     * just narrow what is already complete. */
    const prefixComplete = () => !capped || (state.from !== '' && oldestRecent < state.from);

    function describe(shown, total, counted) {
        const parts = [];
        if (state.from || state.to) {
            parts.push(`dated ${state.from || 'any'} → ${state.to || 'any'}`);
        }
        if (state.variant !== null) parts.push(`filed as “${state.variant}”`);
        if (state.query) parts.push(`matching “${state.query}”`);
        const scope = parts.length ? ` ${parts.join(', ')}` : '';
        const lead = shown < total
            ? `Showing the ${fmt.int(shown)} most recent of ${fmt.int(total)}`
            : `All ${fmt.int(total)}`;
        return `${lead} filed rows${scope} — ${fmt.int(counted)} counted.`;
    }

    async function render() {
        const mine = ++generation;
        if (!full && !untouched() && !prefixComplete()) {
            setStatus(status, 'loading', 'Loading the full history…');
            try {
                const data = await getJSON(url.companyTx(c.lei));
                if (mine !== generation) return;   // a newer view won
                full = data.transactions;
            } catch (err) {
                if (mine !== generation) return;
                setStatus(status, 'error', `Could not load the full history: ${err.message}`);
                return;
            }
        }
        setStatus(status, 'ready', '');

        const source = full || recent;
        /* Only the untouched view reads a capped prefix without it being
         * complete; its totals come from the export instead. */
        const partial = source === recent && capped && !prefixComplete();
        const rows = source.filter(matcher());
        const total = partial ? c.tx_count_total : rows.length;
        const counted = partial ? c.tx_counted_total : rows.filter((t) => t.is_counted).length;
        const shown = state.expanded ? rows : rows.slice(0, limit);

        note.textContent = describe(shown.length, total, counted);
        const bodies = groupRuns(shown).map(runBody);
        if (!bodies.length) {
            const why = state.from && state.to && state.from > state.to
                ? 'The from date is after the to date.'
                : 'No transactions match.';
            bodies.push(el('tbody', {},
                el('tr', {}, el('td', { colspan: TX_COLUMNS, class: 'muted', text: why }))));
        }
        table.replaceChildren(head, ...bodies);

        more.hidden = total <= limit;
        more.textContent = state.expanded
            ? `show the ${fmt.int(limit)} most recent`
            : `show all ${fmt.int(total)}`;
    }

    syncTabs();
    render();
    return { node, pickVariant };
}

async function renderCompany(lei) {
    wireBack('company.html');
    setStatus(statusNode, 'loading', `Loading ${lei}…`);
    let c;
    try {
        c = await getJSON(url.company(lei));
    } catch (err) {
        headingNode.textContent = 'unknown company';
        setStatus(statusNode, 'error',
            `No company with LEI ${lei} (${err.message}).`);
        detailNode.append(el('p', {},
            'Check the LEI, or ',
            el('a', { href: 'company.html', text: 'browse the company index' }),
            '.'));
        return;
    }

    setStatus(statusNode, 'ready', '');
    headingNode.textContent = c.name;
    document.title = `${c.name} — insyn.malmgren.dev`;

    const list = transactionList(c);
    detailNode.append(
        facts(c),
        variants(c, list.pickVariant),
        periods(c),
        list.node,
        el('p', { class: 'prose muted', text: `Computed ${stamp(c.computed_at)}.` }));
}

const lei = param('lei');
if (lei) {
    renderCompany(lei);
} else {
    renderPicker();
}
