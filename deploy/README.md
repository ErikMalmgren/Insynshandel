# Deploying Insynshandel

Two independent paths (§10). Pick one; they share the same pipeline.

| | Static-only | API |
| --- | --- | --- |
| Hosting | GitHub Pages (free) | a €4–6/mo VM or a box at home |
| Runs | `.github/workflows/ingest.yml` | `docker compose` + a systemd timer |
| Frontend `API_BASE` | `'./data'` | `'https://api.example.com/api/v1'` |
| Freshness | per workflow schedule | hourly, served live |

Everything here is **untested until the first backfill lands** — see the ⚠ note
in `ingest.yml` and the standing reminder about `insyn ingest backfill`.

---

## Static-only path

1. Run the backfill once, locally, overnight (see the repo README).
2. `gh release create db-latest data/insynshandel.db --title "Latest database snapshot"`
3. Settings → Pages → Source: **GitHub Actions**, or set up a frontend repo.
4. Wire one of the two publish blocks at the end of `.github/workflows/ingest.yml`.
5. Enable Actions. The schedule takes over.

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
