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

## Run locally

```bash
export TMDB_API_KEY=your_tmdb_key            # required
export PUSHCUT_WEBHOOK=your_pushcut_webhook  # optional; prints to terminal without it
python3 this_weeks_releases.py
```

Flags: `--min-votes N` (default 100), `--movies-only`, `--tv-only`,
`--max-age-days N` (default 365), `--start YYYY-MM-DD --end YYYY-MM-DD`.

## Run in the cloud (every Friday, no laptop)

1. Push this folder to a GitHub repo.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `TMDB_API_KEY`
   - `PUSHCUT_WEBHOOK`
3. The workflow in `.github/workflows/weekly.yml` runs every Friday and pushes to
   your phone. Trigger it any time from **Actions → This Week's Releases → Run
   workflow** (also works from the GitHub mobile app).

## Keys

- **TMDB:** https://www.themoviedb.org/settings/api (free, v3 key)
- **Pushcut:** create a notification named `WeekendMovies` in the app and copy its
  webhook URL.
