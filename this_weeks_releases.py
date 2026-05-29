#!/usr/bin/env python3
"""
This Week's Releases — Rank streaming movies & TV newly released THIS WEEK by IMDb rating.

Run it any day; it auto-finds the current calendar week (Mon–Sun).
Standard library only — no pip install needed.
Optionally pushes the list to your iPhone via Pushcut (set PUSHCUT_WEBHOOK).

Data sources:
  - TMDB         : release-date + streaming-provider filtering  (https://www.themoviedb.org/settings/api)
  - IMDb dataset : the actual IMDb rating + vote count, refreshed DAILY, no key
                   (https://datasets.imdbws.com/ — free for personal use)

Why the IMDb dataset and not OMDb: OMDb caches IMDb data on a lag and is weakest
on brand-new titles — exactly the ones this script surfaces. The official IMDb
daily dump is fresh and needs no API key.

Setup:
  export TMDB_API_KEY=xxxxxxxx        # TMDB v3 API key (the only key needed)

Usage:
  python3 weekend_releases.py                      # this week (Mon–Sun), US
  python3 weekend_releases.py --start 2026-05-22 --end 2026-05-24
  python3 weekend_releases.py --min-votes 50       # hide titles with <50 IMDb votes
  python3 weekend_releases.py --movies-only
  python3 weekend_releases.py --tv-only
"""

import argparse
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

TMDB = "https://api.themoviedb.org/3"
IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
CACHE_DIR = os.path.expanduser("~/.cache/weekend-releases")
CACHE_FILE = os.path.join(CACHE_DIR, "title.ratings.tsv.gz")
CACHE_MAX_AGE_H = 18  # re-download the daily dump at most once per ~day
RESOLVED_TTL_H = 12   # reuse resolved release data for this many hours
REGION = "US"

# Your services -> TMDB US provider IDs. Edit this dict to add/remove services.
PROVIDERS = {
    "Netflix": 8,
    "Apple TV+": 350,
    "Prime Video": 9,
    "Paramount+": 531,
    "Hulu": 15,
    "Peacock": 386,
    "Max": 1899,
    "HBO Max (legacy)": 384,
}
PROVIDER_IDS = "|".join(str(v) for v in PROVIDERS.values())

# Short display names per TMDB provider id (both Max ids collapse to "Max").
PROVIDER_DISPLAY = {
    8: "Netflix", 350: "Apple TV+", 9: "Prime", 531: "Paramount+",
    15: "Hulu", 386: "Peacock", 1899: "Max", 384: "Max",
}

TMDB_KEY = os.environ.get("TMDB_API_KEY")
PUSHCUT_WEBHOOK = os.environ.get("PUSHCUT_WEBHOOK")  # optional: push results to iPhone
PAGE_URL = os.environ.get("PAGE_URL")  # optional: hosted HTML page the notification links to


def send_pushcut(webhook: str, title: str, text: str,
                 actions: list[dict] | None = None,
                 default_url: str | None = None) -> None:
    """Send a notification to the Pushcut iOS app via its webhook.

    actions      -> per-release buttons (each {name, url}) shown on long-press.
    default_url  -> opened when the notification body itself is tapped.
    """
    payload: dict = {"title": title, "text": text}
    if actions:
        payload["actions"] = actions
    if default_url:
        payload["defaultAction"] = {"url": default_url}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "this-weeks-releases/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def fmt_votes(n: int) -> str:
    """Human-friendly vote count: 746 -> '746', 6085 -> '6.1k', 239526 -> '240k'."""
    if n >= 100_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def load_imdb_ratings() -> dict[str, tuple[float, int]]:
    """Download (and cache for a day) IMDb's official ratings dump.

    Returns {imdb_id: (rating, votes)} for every rated title — ~1.5M entries,
    refreshed daily by IMDb. One ~8MB download, then all lookups are local.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    fresh = (os.path.exists(CACHE_FILE)
             and (time.time() - os.path.getmtime(CACHE_FILE)) < CACHE_MAX_AGE_H * 3600)
    if not fresh:
        print("Downloading IMDb daily ratings dump (~8MB)...")
        req = urllib.request.Request(IMDB_RATINGS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        with open(CACHE_FILE, "wb") as fh:
            fh.write(data)
    ratings: dict[str, tuple[float, int]] = {}
    with gzip.open(CACHE_FILE, "rt", encoding="utf-8") as fh:
        next(fh)  # skip header: tconst  averageRating  numVotes
        for line in fh:
            tconst, avg, votes = line.rstrip("\n").split("\t")
            ratings[tconst] = (float(avg), int(votes))
    return ratings


def get_json(url: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": "weekend-releases/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def week_window(today: dt.date) -> tuple[dt.date, dt.date]:
    """The current calendar week: Monday through Sunday containing `today`."""
    monday = today - dt.timedelta(days=today.weekday())  # Monday=0
    return monday, monday + dt.timedelta(days=6)


def tmdb_get(path: str, **params) -> dict:
    params["api_key"] = TMDB_KEY
    return get_json(f"{TMDB}{path}", params)


def discover_movies(start: str, end: str) -> list[dict]:
    """Movies whose DIGITAL/streaming release falls in the window, on our providers."""
    out, page, pages = [], 1, 1
    while page <= pages and page <= 5:
        data = tmdb_get(
            "/discover/movie",
            language="en-US",
            watch_region=REGION,
            with_watch_providers=PROVIDER_IDS,
            with_watch_monetization_types="flatrate",
            with_release_type="4|6",  # 4=Digital, 6=TV
            **{"release_date.gte": start, "release_date.lte": end},
            sort_by="popularity.desc",
            page=page,
        )
        pages = data.get("total_pages", 1)
        for m in data.get("results", []):
            out.append({"kind": "Movie", "tmdb_id": m["id"],
                        "title": m.get("title", "?"),
                        "date": m.get("release_date", ""),
                        "poster": m.get("poster_path") or "",
                        "overview": m.get("overview") or ""})
        page += 1
    return out


def discover_tv(start: str, end: str) -> list[dict]:
    """Candidate shows with any episode airing in the window, on our providers.

    This is a SUPERSET — season_premiere() then keeps only those where a brand-new
    series or a new season actually premieres in the window.
    """
    out, page, pages = [], 1, 1
    while page <= pages and page <= 5:
        data = tmdb_get(
            "/discover/tv",
            language="en-US",
            watch_region=REGION,
            with_watch_providers=PROVIDER_IDS,
            with_watch_monetization_types="flatrate",
            **{"air_date.gte": start, "air_date.lte": end},
            sort_by="popularity.desc",
            page=page,
        )
        pages = data.get("total_pages", 1)
        for s in data.get("results", []):
            out.append({"kind": "TV", "tmdb_id": s["id"],
                        "title": s.get("name", "?"),
                        "date": s.get("first_air_date", ""),
                        "poster": s.get("poster_path") or "",
                        "overview": s.get("overview") or ""})
        page += 1
    return out


def season_premiere(tmdb_id: int, start: str, end: str) -> tuple[int, str] | None:
    """Return (season_number, air_date) if a season premieres in the window, else None.

    season_number 1 == brand-new series; >=2 == new season of an existing show.
    """
    try:
        seasons = tmdb_get(f"/tv/{tmdb_id}").get("seasons", [])
    except Exception:
        return None
    hits = [(s.get("season_number", 0), (s.get("air_date") or "")[:10])
            for s in seasons
            if s.get("season_number", 0) >= 1 and start <= (s.get("air_date") or "")[:10] <= end]
    if not hits:
        return None
    hits.sort(key=lambda x: x[1])
    return hits[0]


def imdb_id_for(kind: str, tmdb_id: int) -> str | None:
    path = f"/movie/{tmdb_id}/external_ids" if kind == "Movie" else f"/tv/{tmdb_id}/external_ids"
    try:
        return tmdb_get(path).get("imdb_id") or None
    except Exception:
        return None


def platforms(kind: str, tmdb_id: int) -> list[dict]:
    """Which of OUR services stream (flatrate) this title in the US.

    Returns [{name, logo}] — logo is a TMDB logo_path (may be "").
    """
    path = f"/movie/{tmdb_id}/watch/providers" if kind == "Movie" else f"/tv/{tmdb_id}/watch/providers"
    try:
        us = tmdb_get(path).get("results", {}).get(REGION, {})
    except Exception:
        return []
    out = []
    for p in us.get("flatrate", []):
        name = PROVIDER_DISPLAY.get(p.get("provider_id"))
        if name and name not in [o["name"] for o in out]:
            out.append({"name": name, "logo": p.get("logo_path") or ""})
    return out


def streaming_date(tmdb_id: int, start: str, end: str) -> str | None:
    """The Digital/TV release date that falls inside the window (prefer US)."""
    try:
        data = tmdb_get(f"/movie/{tmdb_id}/release_dates").get("results", [])
    except Exception:
        return None
    hits = []
    for region in data:
        us = region.get("iso_3166_1") == "US"
        for rd in region.get("release_dates", []):
            if rd.get("type") in (4, 6):
                d = (rd.get("release_date") or "")[:10]
                if start <= d <= end:
                    hits.append((us, d))
    if not hits:
        return None
    hits.sort(key=lambda x: (not x[0], x[1]))  # US first
    return hits[0][1]


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def generate_html(shown: list[dict], start: str, end: str) -> str:
    """Build a self-contained, mobile-first HTML page for the week's releases."""
    def rating_style(r):
        # (text color, faint tint background) — restrained, not a solid block.
        if r is None:
            return "#8b92a0", "rgba(139,146,160,.12)"
        if r >= 7.5:
            return "#5fd08a", "rgba(95,208,138,.13)"
        if r >= 6.0:
            return "#e0b34a", "rgba(224,179,74,.13)"
        return "#e08a7f", "rgba(224,138,127,.13)"

    cards = []
    for it in shown:
        r = it["rating"]
        rating = f"{r:.1f}" if r is not None else "—"
        fg, bg = rating_style(r)
        icon = "📺" if it["kind"] == "TV" else "🎬"

        # Title line: title + (year) · season — all together, season de-emphasized.
        bits = []
        year = (it["date"] or "")[:4]
        if year:
            bits.append(f"({year})")
        if it.get("season"):
            bits.append("New Series" if it["season"] == 1 else f"Season {it['season']}")
        suffix = f' <span class="dim">{_esc(" · ".join(bits))}</span>' if bits else ""

        poster = f"https://image.tmdb.org/t/p/w185{it['poster']}" if it.get("poster") else ""
        poster_el = (f'<div class="poster" style="background-image:url(\'{poster}\')"></div>'
                     if poster else '<div class="poster noimg">🎞️</div>')

        # Neutral pill; the small logo carries the brand color, not the whole pill.
        pills = ""
        for p in it.get("platforms", []):
            logo = (f'<img class="plogo" src="https://image.tmdb.org/t/p/w45{p["logo"]}" alt="">'
                    if p.get("logo") else "")
            pills += f'<span class="pill">{logo}{_esc(p["name"])}</span>'

        imdb = (f'<a class="imdb" href="https://www.imdb.com/title/{it["imdb_id"]}/" '
                f'target="_blank" rel="noopener">IMDb ↗</a>' if it.get("imdb_id") else "")
        overview = _esc((it.get("overview") or "")[:170])
        cards.append(f"""
      <div class="card">
        {poster_el}
        <div class="info">
          <div class="row1">
            <span class="rating" style="color:{fg};background:{bg}">{rating}</span>
            <span class="title">{icon} {_esc(it['title'])}{suffix}</span>
          </div>
          <div class="pills">{pills}</div>
          <div class="overview">{overview}</div>
          <div class="actions">{imdb}<span class="votes">{fmt_votes(it['votes'])} votes · added {it['added'][5:]}</span></div>
        </div>
      </div>""")

    nice_dates = f"{start[5:].replace('-', '/')} – {end[5:].replace('-', '/')}"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>This Week's Releases</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0b0d12; color:#e7e9ee; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ display:flex; align-items:baseline; gap:9px; padding:10px 16px; position:sticky; top:0; z-index:2; background:#0b0d12e8; backdrop-filter:blur(8px); border-bottom:1px solid #1a1e29; }}
  h1 {{ margin:0; font-size:17px; font-weight:700; }}
  .meta {{ color:#828a98; font-size:13px; }}
  main {{ padding:8px 14px 40px; max-width:680px; margin:0 auto; }}
  .card {{ display:flex; align-items:stretch; background:#161a22; border:1px solid #232838; border-radius:14px; overflow:hidden; margin:12px 0; }}
  .poster {{ width:96px; flex:0 0 auto; background:#232838 center/cover no-repeat; }}
  .poster.noimg {{ display:flex; align-items:center; justify-content:center; font-size:30px; color:#5c636f; }}
  .info {{ min-width:0; flex:1; padding:13px 15px; }}
  .row1 {{ display:flex; align-items:center; gap:9px; }}
  .rating {{ font-weight:700; font-size:14px; padding:2px 9px; border-radius:8px; white-space:nowrap; flex:0 0 auto; font-variant-numeric:tabular-nums; }}
  .title {{ font-weight:650; font-size:16px; line-height:1.25; }}
  .dim {{ color:#8b92a0; font-weight:400; font-size:14px; }}
  .pills {{ margin:8px 0; display:flex; gap:6px; flex-wrap:wrap; }}
  .pill {{ display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:500; color:#aab2bf; background:#1a1f2b; border:1px solid #262c3a; padding:3px 10px 3px 4px; border-radius:999px; }}
  .plogo {{ width:18px; height:18px; border-radius:5px; background:#fff; object-fit:contain; padding:1px; }}
  .overview {{ color:#9aa1ad; font-size:13px; line-height:1.4; margin:4px 0 10px; }}
  .actions {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
  .imdb {{ color:#d9b53d; border:1px solid #4a431f; background:transparent; font-weight:600; font-size:12px; text-decoration:none; padding:5px 11px; border-radius:8px; }}
  .votes {{ color:#79808d; font-size:12px; text-align:right; }}
  footer {{ text-align:center; color:#5c636f; font-size:12px; padding:20px; }}
</style></head><body>
<header>
  <h1>🍿 New This Week</h1>
  <span class="meta">{len(shown)} titles · {nice_dates}</span>
</header>
<main>{''.join(cards)}
</main>
<footer>Sources: TMDB (releases) · IMDb daily dataset (ratings)</footer>
</body></html>"""


def resolved_cache_path(start: str, end: str, args) -> str:
    flags = f"{args.max_age_days}-{int(args.movies_only)}-{int(args.tv_only)}"
    return os.path.join(CACHE_DIR, f"resolved_{start}_{end}_{flags}.json")


def resolve_candidates(start: str, end: str, args) -> list[dict]:
    """All the TMDB/IMDb work: discover, filter to genuinely-new, attach ratings,
    posters, and streaming platforms. Returned list is JSON-serializable (cached)."""
    items = []
    if not args.tv_only:
        items += discover_movies(start, end)
    if not args.movies_only:
        items += discover_tv(start, end)

    # De-dupe by (kind, tmdb_id)
    seen, uniq = set(), []
    for it in items:
        key = (it["kind"], it["tmdb_id"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)

    if uniq:
        print(f"Screening {len(uniq)} candidates for brand-new releases this week...")

    # Decide what's genuinely NEW this week, per kind:
    #  - Movies: original release within --max-age-days (drops old catalog re-adds).
    #  - TV: a brand-new series (season 1) or a new season premiering in the window.
    cutoff = ((dt.date.today() - dt.timedelta(days=args.max_age_days)).isoformat()
              if args.max_age_days > 0 else None)
    candidates = []
    for it in uniq:
        if it["kind"] == "Movie":
            if cutoff and not (it["date"] and it["date"] >= cutoff):
                continue  # old catalog film merely re-added to streaming
            it["added"] = streaming_date(it["tmdb_id"], start, end) or it["date"]
            it["season"] = None
            candidates.append(it)
        else:  # TV
            prem = season_premiere(it["tmdb_id"], start, end)
            if not prem:
                continue  # ongoing episodes only — not a new series/season
            it["season"], it["added"] = prem
            candidates.append(it)
        time.sleep(0.02)

    if not candidates:
        return []

    print(f"\n{len(candidates)} brand-new release(s) — matching IMDb ratings...")
    imdb_ratings = load_imdb_ratings()
    print()
    for it in candidates:
        iid = imdb_id_for(it["kind"], it["tmdb_id"])
        it["imdb_id"] = iid
        it["rating"], it["votes"] = imdb_ratings.get(iid, (None, 0)) if iid else (None, 0)
        it["platforms"] = platforms(it["kind"], it["tmdb_id"])
        it["platform"] = "/".join(p["name"] for p in it["platforms"])  # string for terminal/push
        time.sleep(0.02)  # be gentle on TMDB
    return candidates


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank this weekend's streaming releases by IMDb rating.")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides auto weekend)")
    ap.add_argument("--end", help="YYYY-MM-DD (overrides auto weekend)")
    ap.add_argument("--min-votes", type=int, default=100,
                    help="Hide titles with fewer IMDb votes (default 100; use 0 to show all)")
    ap.add_argument("--max-age-days", type=int, default=365,
                    help="Only brand-new titles: original release within N days (0 = no filter)")
    ap.add_argument("--movies-only", action="store_true")
    ap.add_argument("--tv-only", action="store_true")
    ap.add_argument("--html-out", default="index.html",
                    help="Write the HTML page here (default index.html; '' to skip)")
    ap.add_argument("--cache", action="store_true",
                    help="Dev aid: reuse on-disk release data instead of re-fetching "
                         "(for iterating on HTML/format). The pipeline runs without it.")
    args = ap.parse_args()

    if not TMDB_KEY:
        print("ERROR: set the TMDB_API_KEY environment variable.", file=sys.stderr)
        return 1

    if args.start and args.end:
        start, end = args.start, args.end
    else:
        mon, sun = week_window(dt.date.today())
        start, end = mon.isoformat(), sun.isoformat()

    print(f"Week window: {start} → {end}  (region {REGION})")
    print(f"Services: {', '.join(PROVIDERS)}\n")

    # The pipeline fetches fresh every run. --cache is a dev-only shortcut so we
    # can iterate on HTML/formatting without re-hitting the APIs each time.
    cache_path = resolved_cache_path(start, end, args)
    cached = (args.cache and os.path.exists(cache_path)
              and (time.time() - os.path.getmtime(cache_path)) < RESOLVED_TTL_H * 3600)
    if cached:
        with open(cache_path, encoding="utf-8") as fh:
            candidates = json.load(fh)
        age_min = int((time.time() - os.path.getmtime(cache_path)) / 60)
        print(f"[--cache] Loaded {len(candidates)} releases from disk ({age_min}m old).\n")
    else:
        candidates = resolve_candidates(start, end, args)
        if args.cache:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(candidates, fh)

    if not candidates:
        print("\nNo brand-new movies or new series/seasons on your services this week.")
        return 0

    # Apply vote floor (drops unrated/too-new titles, which have 0 votes).
    shown = [it for it in candidates if it["votes"] >= args.min_votes]
    shown.sort(key=lambda x: (x["rating"] or 0, x["votes"]), reverse=True)
    dropped = len(candidates) - len(shown)
    if dropped:
        print(f"(Hid {dropped} title(s) with fewer than {args.min_votes} IMDb votes.)\n")

    if not shown:
        print(f"No titles with at least {args.min_votes} IMDb votes this week. "
              f"Re-run with --min-votes 0 to see everything.")
        return 0

    TW = 40  # title column width
    print(f"{'IMDb':>5}  {'Title':<{TW}}  {'Platform':<18}  {'Type':<5}  {'Added':<10}  {'Votes':>9}")
    print("-" * 96)
    for it in shown:
        rating = f"{it['rating']:.1f}" if it["rating"] is not None else "  -"
        year = (it["date"] or "")[:4]
        title = f"{it['title']} ({year})" if year else it["title"]
        if it.get("season"):  # TV: tag brand-new series vs new season
            title += " — new series" if it["season"] == 1 else f" — S{it['season']}"
        title = title if len(title) <= TW else title[:TW - 1] + "…"
        votes = f"{it['votes']:,}"
        plat = it["platform"] or "—"
        print(f"{rating:>5}  {title:<{TW}}  {plat:<18}  {it['kind']:<5}  {it['added']:<10}  {votes:>9}")
        if it["imdb_id"]:
            print(f"{'':>7}https://www.imdb.com/title/{it['imdb_id']}/")
    print("\n('Added' = date it hit your streaming services this week.)")

    # Write the HTML page (nice mobile layout — the real viewing surface).
    if args.html_out:
        html = generate_html(shown, start, end)
        d = os.path.dirname(args.html_out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.html_out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"Wrote {args.html_out} ({len(shown)} releases).")

    # Push to the phone via Pushcut, if configured.
    if PUSHCUT_WEBHOOK:
        nice_dates = f"{start[5:].replace('-', '/')}–{end[5:].replace('-', '/')}"
        title = f"🍿 {len(shown)} New This Week"
        if PAGE_URL:
            # Hosted page exists: keep the notification short; tap opens the page.
            top = shown[:3]
            preview = "\n".join(
                f"★ {it['rating']:.1f}  {it['title']}" if it["rating"] is not None
                else f"★ —  {it['title']}" for it in top)
            text = f"{nice_dates}\n\n{preview}\n\nTap to see all {len(shown)} →"
            try:
                send_pushcut(PUSHCUT_WEBHOOK, title, text,
                             actions=[{"name": "📋 View full list", "url": PAGE_URL}],
                             default_url=PAGE_URL)
                print("Pushed to your phone via Pushcut (links to page).")
            except Exception as e:
                print(f"WARNING: Pushcut send failed: {e}", file=sys.stderr)
        else:
            # No page yet: fall back to per-release IMDb buttons in the notification.
            blocks, actions = [], []
            for it in shown:
                icon = "📺" if it["kind"] == "TV" else "🎬"
                rating = f"{it['rating']:.1f}" if it["rating"] is not None else "—"
                meta = " · ".join(filter(None, [it["platform"], f"{fmt_votes(it['votes'])} votes"]))
                blocks.append(f"{icon}  ⭐️ {rating}   {it['title']}\n      {meta}")
                if it["imdb_id"]:
                    actions.append({"name": f"▶︎ {it['title']} (IMDb)"[:40],
                                    "url": f"https://www.imdb.com/title/{it['imdb_id']}/"})
            text = f"{nice_dates}\n\n" + "\n\n".join(blocks)
            try:
                send_pushcut(PUSHCUT_WEBHOOK, title, text, actions=actions)
                print(f"Pushed to your phone via Pushcut ({len(actions)} IMDb buttons).")
            except Exception as e:
                print(f"WARNING: Pushcut send failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
