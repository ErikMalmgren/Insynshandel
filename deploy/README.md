# Deploying Insynshandel

One path (§10.1): `ingest.yml` runs on its cron, keeps the `db-latest` release
current, and its `deploy` job publishes to Pages. There is no server — the API
path (FastAPI + Docker) was removed on 2026-09-23.

---

## Static-only path — `insyn.malmgren.dev`

The pipeline publishes JSON; `frontend/` (§16) is the app that reads it. Both
go to GitHub Pages on **this** repo under the `insyn` subdomain.
`www.malmgren.dev` stays exactly as it is and gets one link from
`projects.html`.

Every `ingest.yml` run that passes `doctor` redeploys the site: the `ingest`
job assembles `site/` (§16.2) and uploads it as the Pages artifact, the
`deploy` job publishes it. A failed check stops the run before the upload, so
bad JSON never ships — the previous deploy stays live.

### 1 — Repo and Pages

- [x] Repo is public (Pages on a private repo needs GitHub Pro).
- [x] `ingest.yml`: assemble step, `upload-pages-artifact` (`path: site/`), and
      a `deploy` job with `pages: write` + `id-token: write` (2026-09-22).
- [ ] Settings → Pages → Build and deployment → **Source: GitHub Actions**.
      Do this **before** pushing the workflow change — otherwise the next
      scheduled run's `deploy` job fails (the ingest itself still succeeds).
- [ ] Actions → **ingest** → Run workflow (on `main`). Success = both jobs
      green. **Don't open the link the `deploy` job prints yet:** before the
      custom domain is set it is `erikmalmgren.github.io/Insynshandel/`, which
      redirects to `www.malmgren.dev` (that user site owns the domain), and the
      frontend's root-absolute paths (`/css/…`) only work at a domain root.
      Judge the site only on `insyn.malmgren.dev`, after step 2.
      If `deploy` fails fetching the artifact, add `actions: read` to its
      `permissions:`.

There is no `CNAME` file: with an Actions-based deploy, GitHub takes the custom
domain from Settings → Pages only (step 2 below), and that setting survives
redeploys on its own.

### 2 — Cloudflare DNS — do these in order

The order matters. Cloudflare's proxy blocks GitHub's ACME HTTP-01 validation,
so if you start with the orange cloud on, the certificate never issues and
"Enforce HTTPS" stays greyed out with no error that says why.

- [ ] Cloudflare → `malmgren.dev` → DNS → **Add record**:
      type `CNAME`, name `insyn`, target `erikmalmgren.github.io`,
      **Proxy status: DNS only (grey cloud)**.
- [ ] GitHub → Settings → Pages → **Custom domain** → `insyn.malmgren.dev` →
      Save. Wait for the DNS check to go green.
- [ ] Wait for the certificate — minutes, occasionally up to an hour.
- [ ] Tick **Enforce HTTPS** once it is no longer greyed out.
- [ ] *Optional, and only now:* flip the record back to **Proxied** (orange).
      SSL/TLS encryption mode must be **Full** — Flexible gives a redirect
      loop. **The mode is zone-wide**, and `www` is already proxied, so it is
      already set correctly. Check it; do not change it.

### 3 — Verify

```sh
curl -sI https://insyn.malmgren.dev/ | head -n 20
curl -sI https://insyn.malmgren.dev/data/meta.json | grep -i cache-control
curl -s -o /dev/null -w '%{http_code}\n' https://insyn.malmgren.dev/nope
```

- [ ] The site loads over HTTPS with no certificate warning.
- [ ] `data/meta.json` returns JSON, not a 404.
- [ ] The unknown path returns **404** and shows your `404.html`. A missing
      `404.html` in the artifact fails silently as a generic GitHub page.
- [ ] **Read that `cache-control` value.** If it is short (~600s), §16.7 is
      settled — plain `fetch`, no cache-busting on the JSON. If it is long
      (hours), revisit §16.7 before the hourly data becomes visibly stale.
- [ ] Open the leaderboard, check the console is clean.

### Worth knowing

- **One custom domain per repo.** `www.malmgren.dev` on
  `ErikMalmgren.github.io`, `insyn.malmgren.dev` here — different subdomains on
  different repos is fine and supported.
- **Domain verification** (Settings → Pages → *Verify domains*, which adds a
  `_github-pages-challenge-…` TXT record) stops anyone else claiming the
  subdomain if you ever detach it. Recommended, not required.
- Cloudflare's Scrape Shield email obfuscation is a zone-wide setting that
  already applies to `www` — irrelevant here, since this site publishes no
  addresses, but it is the same zone.

## Recovery

If `data/insynshandel.db` is lost or corrupted:

1. **Restore the backup.** Every successful `ingest.yml` run re-uploads the
   whole DB as the `db-latest` release asset:
   `gh release download db-latest --pattern insynshandel.db --dir data --clobber`
2. **Or rebuild from scratch** — every source is re-fetched live, nothing local
   is needed:
   `insyn db migrate` → `insyn ingest backfill` (~330 requests, 30–45 min) →
   `insyn refdata fx --backfill` → `insyn build` → `insyn doctor`.

`db-latest` is a single rolling copy, overwritten on every run that passes
`doctor`. Corruption that `doctor` does not catch replaces the good copy — then
only option 2 is left.

## Post-backfill checklist

Tracked in the repo's Claude memory; in short:

- `insyn refdata fx --backfill` → `insyn refdata figi` → `insyn refdata marketcaps`
- `insyn build && insyn doctor` — the backfill-gated checks should now run
- build the `issuer_name → lei` recovery map and re-aggregate early history
