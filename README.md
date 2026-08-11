# This Week's Releases

Finds movies & TV that became newly available on **your** streaming services
this week and ranks them by **real IMDb rating**, then pushes the list to your
iPhone via **Pushcut**.

- **Releases data:** TMDB (release dates + which service streams each title)
- **Ratings:** IMDb's official daily dataset (fresh, accurate, no API key)
- **Delivery:** Pushcut iOS app (free tier is fine)
- **Schedule:** GitHub Actions cron — runs in the cloud, no laptop needed

"New this week" = a movie whose original release is recent (within ~1 year) and
hit streaming this week, OR a brand-new TV series / a new season premiering this
week. Ongoing weekly episodes of existing shows are excluded.

## The page

Each run builds a single self-contained HTML page carrying **both this week and
last week**. All three filters live behind the ☰ button at the top right, run
client-side (so switching costs no round-trip), and persist per-device in
`localStorage`:

| Toggle | Default |
| --- | --- |
| Week: Last / This | This |
| Services: Mine / All | **All** |
| Hide Indian titles | **on** |

Each toggle puts its default on the right, so the resting state reads as one column.

Last week is there so a Monday or Tuesday check-in can still see how the previous
weekend's releases ranked.

Every card shows **one** provider row — your services first (highlighted), then
everything else, capped at `PILL_CAP` behind a tappable `+N`. It's deliberately
the same markup in both Services views: rendering a different pill set per view
made cards change height whenever the toggle flipped.

Indian titles are detected from TMDB's `original_language` and `origin_country`.
The logic deliberately errs toward letting one through rather than wrongly hiding
a Pakistani or Bangladeshi title — Urdu and Bengali only count as Indian when TMDB
also reports `IN` as an origin country.

## Run locally

```bash
export TMDB_API_KEY=your_tmdb_key            # required
export PUSHCUT_WEBHOOK=your_pushcut_webhook  # optional; prints to terminal without it
python3 this_weeks_releases.py
```

Flags: `--min-votes N` (default 100), `--movies-only`, `--tv-only`,
`--max-age-days N` (default 365), `--start YYYY-MM-DD --end YYYY-MM-DD`,
`--include-indian` (the terminal list and the Pushcut notification drop Indian
titles by default, matching the page; the page always carries them behind its
toggle).

## Run in the cloud (daily, no laptop)

1. Push this folder to a GitHub repo.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `TMDB_API_KEY`
   - `PUSHCUT_WEBHOOK`
   - `KEEPALIVE_PAT` — see below
3. The workflow in `.github/workflows/weekly.yml` rebuilds the page daily and
   pushes to your phone on Fridays. Trigger it any time from **Actions → This
   Week's Releases → Run workflow** (also works from the GitHub mobile app).

### Why `KEEPALIVE_PAT` exists

GitHub **auto-disables `schedule:` triggers after 60 days of repository
inactivity**, and this repo by design never receives organic commits — so the
daily run dies roughly every two months. That is what happened on 2026-08-06.

The trap: re-enabling the workflow in the UI flips its state back to `active` but
does **not** resume the cron, because workflow runs — including manual
`workflow_dispatch` runs — don't count as repository activity. Only a push does.

So the last step of each run pushes a timestamp to `.last-refresh`, which keeps
the repo permanently "active". It needs a **fine-grained PAT with `Contents:
read and write` scoped to this repo**, stored as `KEEPALIVE_PAT`; the built-in
`GITHUB_TOKEN` attributes its pushes to the Actions bot and isn't a dependable
activity signal. The `push:` trigger has `paths-ignore: ['.last-refresh']` so the
keepalive commit can't loop.

The checkout step needs `persist-credentials: false` for any of that to work.
Otherwise `actions/checkout` leaves a `GITHUB_TOKEN` `Authorization` header in
`.git/config`, git prefers it over the credentials in the push URL, and the
keepalive push goes out as `github-actions[bot]` — a 403 the step swallows as a
warning.

Without the secret the workflow still works — the step logs a warning and skips —
but the schedule will silently die again after 60 days.

## Keys

- **TMDB:** https://www.themoviedb.org/settings/api (free, v3 key)
- **Pushcut:** create a notification named `WeekendMovies` in the app and copy its
  webhook URL.
