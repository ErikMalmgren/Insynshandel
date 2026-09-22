/* about.js — about.html.
 *
 * Coverage and freshness from meta.json, the outlier table from
 * data-quality.json. The counting rules themselves are static prose in the
 * page: they are decisions, not data, and they should be readable even if a
 * fetch fails.
 *
 * No person field is read here — see the header of insyn.js.
 */

import { url, getJSON, fmt, el, clear, setStatus, stamp } from './insyn.js?v=1';

const statusNode = document.getElementById('status');
const coverageNode = document.getElementById('coverage');
const alarmNode = document.getElementById('alarm');
const qualityNode = document.getElementById('quality');

function facts(m) {
    const dl = el('dl', { class: 'kv' });
    const row = (term, ...value) => dl.append(el('dt', { text: term }), el('dd', {}, ...value));

    row('Coverage', `${m.coverage_first_day || '?'} → ${m.coverage_last_day || '?'}`);
    /* A gap means FI's export was never fetched for those days, so the site is
     * quietly missing transactions. Zero is the healthy state and the number
     * is shown either way rather than only when it is bad. */
    row('Missing days', m.missing_days === 0
        ? el('span', { text: '0 — every publication day has been fetched' })
        : el('span', { class: 'neg', text: `${fmt.int(m.missing_days)} — some days were never fetched` }));
    row('Longest gap', `${fmt.int(m.longest_gap_days)} days`);
    row('Last ingest', stamp(m.last_ingest_at));
    row('Rows filed', `${fmt.int(m.raw_live_rows)} live rows from FI`);
    row('Rows counted', `${fmt.int(m.counted_rows)} of ${fmt.int(m.norm_rows)} `
        + `(${fmt.pct(m.counted_rows / m.norm_rows).replace('+', '')})`);
    row('Market caps', `${fmt.int(m.market_caps_known)} issuers have one`);
    row('Currencies', m.currencies.join(' '));
    row('Verification',
        `${fmt.int(m.verification.ok || 0)} ok · `
        + `${fmt.int(m.verification.unverifiable || 0)} unverifiable · `
        + `${fmt.int(m.verification.outlier || 0)} outlier`);
    row('Computed', stamp(m.computed_at));
    return dl;
}

/* Invariant 7: an unmapped Karaktär value fails the build, so an empty object
 * is the only normal state. If one ever reaches the browser it means the site
 * is serving data the pipeline could not classify — render it loudly rather
 * than as one more field in a list nobody reads. */
function alarm(m) {
    const natures = Object.entries(m.unmapped_natures || {});
    if (!natures.length) return;
    alarmNode.hidden = false;
    alarmNode.append(
        el('h3', { text: 'Unclassified transaction types' }),
        el('p', { text: 'The pipeline met Karaktär values it has no rule for. Rows with these '
            + 'values are not counted, and the totals on this site are wrong until '
            + 'data/seed/nature_map.csv gains a row for each:' }),
        el('ul', {}, natures.map(([nature, n]) =>
            el('li', { text: `${nature} — ${fmt.int(n)} rows` }))));
}

function quality(dq) {
    const head = el('tr', {}, [
        el('th', { scope: 'col', text: 'company' }),
        el('th', { scope: 'col', text: 'date' }),
        el('th', { scope: 'col', text: 'nature' }),
        el('th', { scope: 'col', class: 'num', text: 'volume' }),
        el('th', { scope: 'col', class: 'num', text: 'price' }),
        el('th', { scope: 'col', class: 'opt', text: 'ccy' }),
        el('th', { scope: 'col', class: 'num', text: 'value SEK' }),
        el('th', { scope: 'col', class: 'num opt', text: 'market cap' }),
        el('th', { scope: 'col', text: 'treated as' }),
        el('th', { scope: 'col', text: 'source' }),
    ]);

    const rows = dq.outliers.map((o) => el('tr', {}, [
        el('td', { class: 'col-name' },
            el('a', { href: `company.html?lei=${encodeURIComponent(o.lei)}`, text: o.name })),
        el('td', { text: o.transaction_date }),
        el('td', { text: o.nature }),
        el('td', { class: 'num', text: o.volume === null ? '—' : fmt.full(o.volume) }),
        el('td', { class: 'num', text: o.price === null ? '—' : fmt.price(o.price) }),
        el('td', { class: 'opt muted', text: o.currency }),
        el('td', {
            class: 'num',
            text: o.gross_value_sek === null ? '—' : fmt.compact(o.gross_value_sek),
            title: o.gross_value_sek === null ? null : `${fmt.full(o.gross_value_sek)} SEK`,
        }),
        el('td', {
            class: 'num opt',
            text: o.market_cap === null ? '—' : fmt.compact(o.market_cap),
            title: o.market_cap === null ? 'no market cap for this issuer' : `${fmt.full(o.market_cap)} SEK`,
        }),
        el('td', { class: 'muted', text: o.exclude_reason || 'counted' }),
        el('td', {}, el('a', {
            href: o.fi_search_url, rel: 'noopener', text: 'FI',
            title: 'the same search at Finansinspektionen',
        })),
    ]));

    qualityNode.append(
        el('p', { class: 'prose' },
            `${fmt.int(dq.outlier_count)} rows are implausible enough to report. `,
            'Each links back to the same search at Finansinspektionen, so the filed '
            + 'row can be read as FI published it.'),
        el('p', { class: 'prose muted' },
            `${fmt.int(dq.contested_isin_count)} ISINs are filed under more than one LEI. `
            + 'FI ships wrong LEIs as well as missing ones, and this site aggregates on '
            + 'LEI — so a contested ISIN is a company that may be split across, or merged '
            + 'into, the wrong row on the leaderboard.'),
        el('div', { class: 'scroller' },
            el('table', {}, el('thead', {}, head), el('tbody', {}, rows))));
}

async function load() {
    setStatus(statusNode, 'loading', 'Loading coverage and data quality…');
    try {
        const [m, dq] = await Promise.all([
            getJSON(url.meta()),
            getJSON(url.dataQuality()),
        ]);
        setStatus(statusNode, 'ready', '');
        clear(coverageNode);
        coverageNode.append(facts(m));
        alarm(m);
        quality(dq);
    } catch (err) {
        setStatus(statusNode, 'error', `Could not load the dataset summary: ${err.message}`);
    }
}

load();
