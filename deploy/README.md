# Deploying Insynshandel

Two independent paths (§10). Pick one; they share the same pipeline.

| | Static-only | API |
| --- | --- | --- |
| Hosting | GitHub Pages (free) | a €4–6/mo VM or a box at home |
| Runs | `.github/workflows/ingest.yml` | `docker compose` + a systemd timer |
| Frontend `API_BASE` | `'./data'` | `'https://api.example.com/api/v1'` |
| Freshness | per workflow schedule | hourly, served live |

The **API path** below is untested until the first backfill lands — see the ⚠
note in `ingest.yml` and the standing reminder about `insyn ingest backfill`.
The static path's Pages and DNS plumbing can be wired and verified before then,
against whatever data the working database already holds.

---

## Static-only path — `insyn.malmgren.dev`

The pipeline publishes JSON; `frontend/` (§16) is the app that reads it. Both
go to GitHub Pages on **this** repo under the `insyn` subdomain.
`www.malmgren.dev` stays exactly as it is and gets one link from
`projects.html`.

> **Prerequisite:** the ingest workflow has to succeed before Pages has
> anything to publish. It currently does not — see the ⚠ header in
> `.github/workflows/ingest.yml`. Run the backfill and seed the `db-latest`
> release first, or disable the `schedule:` block while you build the frontend.

### 1 — Repo and Pages

- [ ] **Confirm this repo is public.** Pages on a private repo needs GitHub Pro.
- [ ] Create `frontend/CNAME` containing exactly `insyn.malmgren.dev`. It must
      end up **in the uploaded artifact** — that is what survives a redeploy.
- [ ] In `.github/workflows/ingest.yml`, add to `permissions:`
      `pages: write` and `id-token: write`.
- [ ] Wire publish block (i): the assemble step from §16.2, then
      `actions/upload-pages-artifact@v3` with `path: site/`, then
      `actions/deploy-pages@v4`.
- [ ] Settings → Pages → **Source: GitHub Actions**.
- [ ] Run the workflow once (`workflow_dispatch`) and confirm it deploys.

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

## API path (Docker, one host)

```sh
git clone https://github.com/ErikMalmgren/Insynshandel.git /opt/insynshandel
cd /opt/insynshandel
cp .env.example .env            # set OPENFIGI_API_KEY, INSYN_CORS_ORIGINS

# one-time: create the database on the volume
docker compose run --rm cli db migrate
docker compose run --rm cli ingest backfill      # ~30–45 min, overnight
docker compose run --rm cli build

# always-on reader
docker compose up -d api

# scheduled writer
cp deploy/systemd/insyn-ingest.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now insyn-ingest.timer
```

Then put `deploy/Caddyfile` in front (or a Cloudflare/Tailscale tunnel if you
are behind CGNAT — §10.2.1) and point it at `127.0.0.1:8000`.

### Rules that matter more than the config (§10.4)

1. **The DB is a host bind mount on local disk.** Not a named volume (a rebuild
   would lose it), not a NAS share (SQLite WAL corrupts on NFS/SMB — §10.2.2).
2. **The API connection is read-only** (`mode=ro`), so multi-worker uvicorn is
   safe and an API bug can never take a write lock.
3. **The ingest is single-writer.** Never run two writers against one file;
   never run the ingest inside a uvicorn worker.

### Backup

`litestream replicate` the DB file to object storage (or rsync to a NAS —
off the live disk). `VACUUM` once after the backfill.

## Post-backfill checklist

Tracked in the repo's Claude memory; in short:

- `insyn refdata fx --backfill` → `insyn refdata figi` → `insyn refdata marketcaps`
- `insyn build && insyn doctor` — the backfill-gated checks should now run
- §11.3 parity vs `python script.py`, then delete the four legacy root scripts
- build the `issuer_name → lei` recovery map and re-aggregate early history
