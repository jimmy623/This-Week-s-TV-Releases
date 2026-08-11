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

# TMDB NETWORK ids for our services -> display name. Networks are editorial
# metadata, populated when a show is announced — unlike watch-providers (sourced
# from JustWatch) which lag for brand-new titles. We use this as a fallback so a
# launch-week premiere on one of our services isn't missed just because its
# provider data hasn't landed yet (e.g. Cape Fear on Apple TV+).
NETWORK_DISPLAY = {
    213: "Netflix", 2552: "Apple TV+", 1024: "Prime", 4330: "Paramount+",
    3353: "Peacock", 453: "Hulu", 3186: "Max", 49: "Max",
}

# ISO 639-1 codes for languages spoken (essentially) only in India. A title in one
# of these is Indian regardless of what TMDB says about its origin country — which
# matters for movies, where /discover often omits origin_country entirely.
INDIAN_LANGS = {
    "hi", "ta", "te", "ml", "kn", "mr", "pa", "gu", "or", "as",
    "sa", "bh", "kok", "mai", "doi", "mni", "sat", "ks", "sd",
}

# Shared with neighbouring countries (Urdu -> Pakistan, Bengali -> Bangladesh), so
# these only count as Indian when TMDB independently reports IN as an origin
# country. Guessing here would wrongly hide Pakistani and Bangladeshi titles.
INDIAN_SHARED_LANGS = {"ur", "bn"}

# Providers shown on a card before the rest collapse behind a "+N" pill. Bounds
# the pill row to ~2 lines on a phone; without it a title on nine services makes
# its card half again as tall as its neighbours.
PILL_CAP = 4

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


def is_indian(it: dict) -> bool:
    """Whether a title is Indian, for the page's default-on 'Hide Indian titles' filter.

    Two signals, because neither is reliable alone: TMDB gives origin_country for TV
    but frequently omits it for movies, while original_language misses Indian
    productions shot in English. We deliberately bias toward false NEGATIVES (an
    occasional Indian title slips through) over false positives (silently hiding a
    Pakistani or Bangladeshi title the user never asked to filter).
    """
    if "IN" in (it.get("origin_country") or []):
        return True
    lang = it.get("original_language") or ""
    if lang in INDIAN_LANGS:
        return True
    return lang in INDIAN_SHARED_LANGS and "IN" in (it.get("origin_country") or [])


def tmdb_get(path: str, **params) -> dict:
    params["api_key"] = TMDB_KEY
    return get_json(f"{TMDB}{path}", params)


def _discover_movie_pages(start: str, end: str, restrict: bool) -> list[dict]:
    """One /discover/movie query (paginated, up to 5 pages).

    restrict=True  -> only films on OUR services (watch-provider filter). This is
                      the accurate population for the 'My Services' view — it
                      includes e.g. foreign films on Netflix US that a plain
                      region=US release-type query misses.
    restrict=False -> every film with a US digital/TV release this week, for the
                      page's 'All' view.
    """
    flt = (dict(watch_region=REGION, with_watch_providers=PROVIDER_IDS,
                with_watch_monetization_types="flatrate") if restrict
           else dict(region=REGION))
    out, page, pages = [], 1, 1
    while page <= pages and page <= 5:
        data = tmdb_get(
            "/discover/movie",
            language="en-US",
            with_release_type="4|6",  # 4=Digital, 6=TV
            **{"release_date.gte": start, "release_date.lte": end},
            sort_by="popularity.desc",
            page=page,
            **flt,
        )
        pages = data.get("total_pages", 1)
        for m in data.get("results", []):
            out.append({"kind": "Movie", "tmdb_id": m["id"],
                        "title": m.get("title", "?"),
                        "date": m.get("release_date", ""),
                        "poster": m.get("poster_path") or "",
                        "overview": m.get("overview") or "",
                        "genre_ids": m.get("genre_ids", []),
                        "original_language": m.get("original_language") or "",
                        "origin_country": m.get("origin_country") or []})
        page += 1
    return out


def discover_movies(start: str, end: str) -> list[dict]:
    """Movies with a DIGITAL/streaming release in the window.

    Two passes (resolve_candidates() de-dupes): provider-filtered for an accurate
    'My Services' list, plus unfiltered for the 'All' view.
    """
    out = _discover_movie_pages(start, end, restrict=True)
    out += _discover_movie_pages(start, end, restrict=False)
    return out


def _discover_tv_pages(date_params: dict, restrict: bool) -> list[dict]:
    """Run one /discover/tv query (paginated, up to 5 pages).

    restrict=True  -> only shows on OUR services (watch-provider filter).
    restrict=False -> every show, for the 'All' view. This pass also catches
                      brand-new premieres whose provider data hasn't landed yet
                      (e.g. Cape Fear) — they get matched to our services later in
                      watch_info() via the network fallback.
    """
    flt = (dict(watch_region=REGION, with_watch_providers=PROVIDER_IDS,
                with_watch_monetization_types="flatrate") if restrict else {})
    out, page, pages = [], 1, 1
    while page <= pages and page <= 5:
        data = tmdb_get(
            "/discover/tv",
            language="en-US",
            **date_params,
            sort_by="popularity.desc",
            page=page,
            **flt,
        )
        pages = data.get("total_pages", 1)
        for s in data.get("results", []):
            out.append({"kind": "TV", "tmdb_id": s["id"],
                        "title": s.get("name", "?"),
                        "date": s.get("first_air_date", ""),
                        "poster": s.get("poster_path") or "",
                        "overview": s.get("overview") or "",
                        "genre_ids": s.get("genre_ids", []),
                        "original_language": s.get("original_language") or "",
                        "origin_country": s.get("origin_country") or []})
        page += 1
    return out


def discover_tv(start: str, end: str) -> list[dict]:
    """Candidate shows for the window.

    This is a SUPERSET — season_premiere() then keeps only those where a brand-new
    series or a new season actually premieres in the window. We run several passes
    and merge them (resolve_candidates() de-dupes by tmdb_id):

      - air_date in window       : new SEASONS of existing shows (episodes airing
                                   this week).
      - first_air_date in window : BRAND-NEW series. TMDB's episode-level air_date
                                   index misses some premieres (a S1 only reachable
                                   via first_air_date), so air_date alone drops them.

    Each is run provider-filtered (accurate 'My Services' population, incl. shows
    that rank low globally) AND unfiltered (the 'All' view, and no-provider
    premieres like Cape Fear).
    """
    a = {"air_date.gte": start, "air_date.lte": end}
    f = {"first_air_date.gte": start, "first_air_date.lte": end}
    out = _discover_tv_pages(a, restrict=True)
    out += _discover_tv_pages(f, restrict=True)
    out += _discover_tv_pages(a, restrict=False)
    out += _discover_tv_pages(f, restrict=False)
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


def genre_maps() -> tuple[dict, dict]:
    """One-time {id: name} maps for movie and TV genres (lists differ slightly)."""
    try:
        movie = {x["id"]: x["name"] for x in tmdb_get("/genre/movie/list").get("genres", [])}
        tv = {x["id"]: x["name"] for x in tmdb_get("/genre/tv/list").get("genres", [])}
        return movie, tv
    except Exception:
        return {}, {}


def imdb_id_for(kind: str, tmdb_id: int) -> str | None:
    path = f"/movie/{tmdb_id}/external_ids" if kind == "Movie" else f"/tv/{tmdb_id}/external_ids"
    try:
        return tmdb_get(path).get("imdb_id") or None
    except Exception:
        return None


def tv_networks_mine(tmdb_id: int) -> list[dict]:
    """Fallback for brand-new shows: map the show's TMDB networks to OUR services.

    TMDB has the NETWORK (editorial, set when announced) even when JustWatch-sourced
    watch-providers are still empty — so this catches launch-week premieres whose
    provider data hasn't landed yet. Returns [{name, logo}].
    """
    try:
        nets = tmdb_get(f"/tv/{tmdb_id}").get("networks", [])
    except Exception:
        return []
    out = []
    for n in nets:
        name = NETWORK_DISPLAY.get(n.get("id"))
        if name and name not in [o["name"] for o in out]:
            out.append({"name": name, "logo": n.get("logo_path") or ""})
    return out


def watch_info(kind: str, tmdb_id: int) -> tuple[list[dict], list[dict]]:
    """US flatrate streaming for a title, as (mine, providers).

    mine      -> [{name, logo}] limited to OUR services, with a network fallback
                 for brand-new shows whose provider data hasn't landed yet.
    providers -> EVERY US flatrate service as [{name, logo, mine}], ours first.

    Deliberately one merged list rather than two: the card renders the same pill
    row in both views, so flipping My Services <-> All can't change a card's
    height. Ours keep their short display names ("Prime"), the rest keep TMDB's.
    """
    path = f"/movie/{tmdb_id}/watch/providers" if kind == "Movie" else f"/tv/{tmdb_id}/watch/providers"
    try:
        us = tmdb_get(path).get("results", {}).get(REGION, {})
    except Exception:
        us = {}
    mine: list[dict] = []
    others: list[dict] = []
    for p in us.get("flatrate", []):
        logo = p.get("logo_path") or ""
        name = PROVIDER_DISPLAY.get(p.get("provider_id"))
        bucket, label = (mine, name) if name else (others, p.get("provider_name") or "")
        if label and label not in [o["name"] for o in bucket]:
            bucket.append({"name": label, "logo": logo})
    if not mine and kind == "TV":  # provider data not in yet — try the network
        mine = tv_networks_mine(tmdb_id)
    providers = ([dict(o, mine=True) for o in mine]
                 + [dict(o, mine=False) for o in others])
    return mine, providers


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


def _rating_style(r: float | None) -> tuple[str, str]:
    """(text color, faint tint background) — restrained, not a solid block."""
    if r is None:
        return "#8b92a0", "rgba(139,146,160,.12)"
    if r >= 7.5:
        return "#5fd08a", "rgba(95,208,138,.13)"
    if r >= 6.0:
        return "#e0b34a", "rgba(224,179,74,.13)"
    return "#e08a7f", "rgba(224,138,127,.13)"


def _card_html(it: dict, week_key: str) -> str:
    """One release card.

    Every card for both weeks is emitted into the same page; the data-* attributes
    (week / mine / indian) are what the three client-side toggles filter on.
    """
    r = it["rating"]
    rating = f"{r:.1f}" if r is not None else "—"
    fg, bg = _rating_style(r)
    icon = "📺" if it["kind"] == "TV" else "🎬"

    # Title line: title + (year) · season — all together, season de-emphasized.
    bits = []
    year = (it["date"] or "")[:4]
    if year:
        bits.append(f"({year})")
    if it.get("season"):
        bits.append("New Series" if it["season"] == 1 else f"Season {it['season']}")
    suffix = f' <span class="dim">{_esc(" · ".join(bits))}</span>' if bits else ""

    url = f"https://www.imdb.com/title/{it['imdb_id']}/" if it.get("imdb_id") else ""
    poster = f"https://image.tmdb.org/t/p/w185{it['poster']}" if it.get("poster") else ""
    pstyle = f' style="background-image:url(\'{poster}\')"' if poster else ""
    pclass = "poster" if poster else "poster noimg"
    pinner = "" if poster else "🎞️"
    # Date chip (with weekday) overlaid on the poster's empty corner.
    try:
        d = dt.date.fromisoformat(it["added"][:10])
        date_label = f"{d:%a} {d.month}/{d.day}"
    except Exception:
        date_label = it["added"][5:]
    chip = f'<span class="pdate">{date_label}</span>'
    if url:
        poster_el = f'<a class="{pclass}" href="{url}" target="_blank" rel="noopener"{pstyle}>{pinner}{chip}</a>'
    else:
        poster_el = f'<div class="{pclass}"{pstyle}>{pinner}{chip}</div>'

    # Neutral pill; the small logo carries the brand color, not the whole pill.
    # ONE row for both views — our services first (subtly highlighted), then the
    # rest, capped at PILL_CAP behind a "+N". Identical markup either way, so the
    # My Services / All toggle changes which cards show, never how tall they are.
    provs = it.get("all_providers") or []
    pills = ""
    for i, p in enumerate(provs):
        cls = "pill"
        logo = ""
        if p.get("logo"):
            cls += " logo"
            logo = (f'<img class="plogo" loading="lazy" alt="" '
                    f'src="https://image.tmdb.org/t/p/w45{p["logo"]}">')
        if p.get("mine"):
            cls += " mine"
        if i >= PILL_CAP:
            cls += " extra"
        pills += f'<span class="{cls}">{logo}{_esc(p["name"])}</span>'
    if len(provs) > PILL_CAP:
        n_more = len(provs) - PILL_CAP
        pills += (f'<button class="pill more" type="button" '
                  f'aria-label="Show {n_more} more">+{n_more}</button>')
    pills = f'<div class="pills">{pills}</div>' if pills else ""

    # Up to 3 genre tags.
    tags = "".join(f'<span class="tag">{_esc(gname)}</span>'
                   for gname in it.get("genres", [])[:3])
    tags = f'<div class="tags">{tags}</div>' if tags else ""

    title_inner = f'{icon} {_esc(it["title"])}{suffix}'
    title_el = (f'<a class="title" href="{url}" target="_blank" rel="noopener">{title_inner}</a>'
                if url else f'<span class="title">{title_inner}</span>')
    overview = _esc((it.get("overview") or "")[:170])
    mine_attr = "1" if it.get("on_my_services") else "0"
    indian_attr = "1" if it.get("is_indian") else "0"
    return f"""
      <div class="card" data-week="{week_key}" data-mine="{mine_attr}" data-indian="{indian_attr}">
        {poster_el}
        <div class="info">
          <div class="row1">
            <span class="rating" style="color:{fg};background:{bg}">{rating}</span>
            {title_el}
          </div>
          {pills}
          {tags}
          <div class="overview">{overview}</div>
        </div>
      </div>"""


def nice_range(start: str, end: str) -> str:
    """'2026-08-10','2026-08-16' -> '8/10 – 8/16'."""
    return f"{start[5:].replace('-', '/')} – {end[5:].replace('-', '/')}"


def generate_html(weeks: list[dict], generated: str, generated_iso: str) -> str:
    """Build a self-contained, mobile-first HTML page for the releases.

    `weeks` is [{key, label, start, end, items}] — currently this week and last
    week. Both weeks' cards are emitted into the one page and the header toggle
    swaps between them client-side, so switching weeks costs no network round-trip
    and works from the cached page offline.

    The footer shows when the data was refreshed, so you can tell at a glance
    whether a (manual or scheduled) rebuild actually landed. `generated` is the
    UTC fallback text; `generated_iso` is the UTC instant (ISO 8601) the browser
    reformats into the viewer's own timezone via JS.
    """
    cards = [_card_html(it, w["key"]) for w in weeks for it in w["items"]]
    dates_json = json.dumps({w["key"]: nice_range(w["start"], w["end"]) for w in weeks})
    first_dates = nice_range(weeks[0]["start"], weeks[0]["end"])
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>This Week's Releases</title>
<style>
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust:100%; }}
  body {{ margin:0; background:#0b0d12; color:#e7e9ee; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; -webkit-font-smoothing:antialiased; }}
  a {{ -webkit-tap-highlight-color:rgba(255,255,255,.06); }}
  /* nowrap throughout: the header is sticky, so anything that wraps changes its
     height and shoves the whole list down. Long metadata ellipsises instead. */
  header {{ display:flex; align-items:center; gap:8px; flex-wrap:nowrap; padding:11px 14px; position:sticky; top:0; z-index:2; background:#0b0d12e8; backdrop-filter:blur(8px); border-bottom:1px solid #1a1e29; padding-top:max(11px, env(safe-area-inset-top)); }}
  h1 {{ margin:0; font-size:17px; font-weight:700; white-space:nowrap; flex:0 0 auto; }}
  .meta {{ color:#828a98; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }}
  main {{ padding:6px 11px 40px; max-width:680px; margin:0 auto; }}
  .card {{ display:flex; align-items:stretch; background:#161a22; border:1px solid #232838; border-radius:14px; overflow:hidden; margin:10px 0; }}
  .poster {{ width:34vw; max-width:118px; min-width:96px; flex:0 0 auto; background:#232838 center/cover no-repeat; display:block; position:relative; }}
  .poster.noimg {{ display:flex; align-items:center; justify-content:center; font-size:30px; color:#5c636f; text-decoration:none; }}
  .pdate {{ position:absolute; left:6px; bottom:6px; background:rgba(0,0,0,.62); color:#eef1f6; font-size:11px; font-weight:600; padding:2px 7px; border-radius:6px; backdrop-filter:blur(2px); }}
  .info {{ min-width:0; flex:1; padding:11px 13px; display:flex; flex-direction:column; }}
  .row1 {{ display:flex; align-items:flex-start; gap:8px; }}
  .rating {{ font-weight:700; font-size:14px; padding:2px 8px; border-radius:7px; white-space:nowrap; flex:0 0 auto; font-variant-numeric:tabular-nums; margin-top:1px; }}
  .title {{ flex:1; min-width:0; font-weight:650; font-size:15.5px; line-height:1.3; overflow-wrap:anywhere; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
  a.title {{ color:inherit; text-decoration:none; }}
  .dim {{ color:#8b92a0; font-weight:400; font-size:13.5px; }}
  .pills {{ margin:7px 0 0; display:flex; gap:6px; flex-wrap:wrap; }}
  .pill {{ display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:500; color:#8f97a4; background:#1a1f2b; border:1px solid #262c3a; padding:3px 9px; border-radius:999px; }}
  .pill.logo {{ padding-left:4px; }}
  .pill.mine {{ color:#cfd6e2; background:#1d2434; border-color:#33405c; }}  /* one of yours */
  .plogo {{ width:17px; height:17px; border-radius:5px; background:#fff; object-fit:contain; padding:1px; }}
  /* Overflow beyond PILL_CAP, revealed by tapping the "+N" pill. */
  .pill.extra {{ display:none; }}
  .pills.expanded .pill.extra {{ display:inline-flex; }}
  .pills.expanded .pill.more {{ display:none; }}
  .pill.more {{ appearance:none; -webkit-appearance:none; margin:0; font-family:inherit; font-size:12px; font-weight:600; cursor:pointer; }}
  .tags {{ margin:7px 0 0; display:flex; gap:6px; flex-wrap:wrap; }}
  .tag {{ font-size:11px; color:#9aa1ad; border:1px solid #2c3342; border-radius:6px; padding:2px 7px; }}
  .overview {{ color:#9aa1ad; font-size:13px; line-height:1.42; margin:7px 0 0; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; }}
  footer {{ text-align:center; color:#5c636f; font-size:12px; padding:8px 20px 28px; }}
  .htext {{ display:flex; align-items:baseline; gap:8px; flex-wrap:nowrap; min-width:0; overflow:hidden; }}
  .empty {{ display:none; text-align:center; color:#5c636f; font-size:14px; padding:36px 20px; }}

  /* All three filters live in the top-right menu, so the header stays one line. */
  .iconbtn {{ position:relative; margin-left:auto; flex:0 0 auto; appearance:none; -webkit-appearance:none; display:inline-flex; align-items:center; justify-content:center; width:36px; height:36px; padding:0; background:#11151d; border:1px solid #232838; border-radius:10px; color:#c3cad6; cursor:pointer; }}
  .iconbtn svg {{ width:18px; height:18px; display:block; }}
  .iconbtn[aria-expanded="true"] {{ background:#2a3142; border-color:#39415a; color:#e7e9ee; }}
  /* A filter is set to something you can't infer from the header. Deliberately a
     fixed-size dot on a fixed-size button, not text — text here would re-wrap the
     header. Week isn't included: the date range already gives it away. */
  .iconbtn::after {{ content:""; position:absolute; top:5px; right:5px; width:6px; height:6px; border-radius:50%; background:#3d6be0; opacity:0; }}
  body.view-mine .iconbtn::after, body:not(.hide-indian) .iconbtn::after {{ opacity:1; }}
  .menu {{ position:absolute; top:calc(100% + 8px); right:14px; z-index:3; min-width:238px; background:#131721; border:1px solid #2a3142; border-radius:14px; padding:4px 6px; box-shadow:0 16px 38px rgba(0,0,0,.55); }}
  .menu[hidden] {{ display:none; }}
  .mrow {{ display:flex; align-items:center; justify-content:space-between; gap:14px; padding:9px 6px; }}
  .mrow + .mrow {{ border-top:1px solid #1e2431; }}
  .mlabel {{ color:#9aa1ad; font-size:13px; font-weight:500; white-space:nowrap; }}
  .toggle {{ display:inline-flex; background:#11151d; border:1px solid #232838; border-radius:999px; padding:2px; flex:0 0 auto; }}
  .toggle button {{ appearance:none; -webkit-appearance:none; border:0; background:transparent; color:#9aa1ad; font:inherit; font-size:12.5px; font-weight:600; padding:5px 12px; border-radius:999px; cursor:pointer; }}
  .toggle button.active {{ background:#2a3142; color:#e7e9ee; }}
  .switch {{ cursor:pointer; user-select:none; -webkit-user-select:none; }}
  .switch input {{ appearance:none; -webkit-appearance:none; margin:0; width:34px; height:20px; border-radius:999px; background:#2a3142; position:relative; flex:0 0 auto; transition:background .15s; cursor:pointer; }}
  .switch input::after {{ content:""; position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#e7e9ee; transition:transform .15s; }}
  .switch input:checked {{ background:#3d6be0; }}
  .switch input:checked::after {{ transform:translateX(14px); }}

  body.view-mine .card[data-mine="0"] {{ display:none; }}
  body.hide-indian .card[data-indian="1"] {{ display:none; }}
  body.week-this .card[data-week="last"] {{ display:none; }}
  body.week-last .card[data-week="this"] {{ display:none; }}
</style></head><body class="view-all week-this hide-indian">
<header>
  <div class="htext">
    <h1>🍿 New Releases</h1>
    <span class="meta"><span id="count">0</span> titles · <span id="dates">{first_dates}</span></span>
  </div>
  <button id="menu-btn" class="iconbtn" type="button" aria-label="Filters"
          aria-haspopup="true" aria-expanded="false" aria-controls="menu">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
  </button>
  <div id="menu" class="menu" role="group" aria-label="Filters" hidden>
    <div class="mrow">
      <span class="mlabel">Week</span>
      <div class="toggle">
        <button id="tab-last" type="button">Last</button>
        <button id="tab-this" class="active" type="button">This</button>
      </div>
    </div>
    <div class="mrow">
      <span class="mlabel">Services</span>
      <div class="toggle">
        <button id="tab-mine" type="button">Mine</button>
        <button id="tab-all" class="active" type="button">All</button>
      </div>
    </div>
    <label class="mrow switch">
      <span class="mlabel">Hide Indian titles</span>
      <input type="checkbox" id="hide-indian" checked>
    </label>
  </div>
</header>
<main>{''.join(cards)}
  <p id="empty" class="empty">Nothing matches these filters.</p>
</main>
<footer>Refreshed <span id="refreshed" data-ts="{generated_iso}">{generated}</span><br>Sources: TMDB (releases) · IMDb daily dataset (ratings)</footer>
<script>
(function() {{
  var b = document.body, DATES = {dates_json};
  function el(id) {{ return document.getElementById(id); }}

  // Toggle state persists per-device, so the defaults below only apply on a first
  // visit. Wrapped in try/catch: Safari private browsing can throw on localStorage.
  function pref(k, d) {{
    try {{ var v = localStorage.getItem(k); return v === null ? d : v === '1'; }}
    catch (e) {{ return d; }}
  }}
  function save(k, v) {{ try {{ localStorage.setItem(k, v ? '1' : '0'); }} catch (e) {{}} }}

  var showAll  = pref('twr.all',  true),   // default: All services
      lastWeek = pref('twr.last', false),  // default: this week
      noIndian = pref('twr.noin', true);   // default: hide Indian titles

  var cards = [].slice.call(document.querySelectorAll('.card')),
      tabMine = el('tab-mine'), tabAll = el('tab-all'),
      tabThis = el('tab-this'), tabLast = el('tab-last'),
      chk = el('hide-indian'), countEl = el('count'),
      datesEl = el('dates'), emptyEl = el('empty'),
      menuEl = el('menu'), menuBtn = el('menu-btn');

  // Single source of truth: CSS does the hiding, this recomputes the visible
  // count with the same three predicates so the header can never disagree.
  function apply() {{
    var wk = lastWeek ? 'last' : 'this';
    b.classList.toggle('view-all', showAll);
    b.classList.toggle('view-mine', !showAll);
    b.classList.toggle('week-last', lastWeek);
    b.classList.toggle('week-this', !lastWeek);
    b.classList.toggle('hide-indian', noIndian);
    tabAll.classList.toggle('active', showAll);
    tabMine.classList.toggle('active', !showAll);
    tabLast.classList.toggle('active', lastWeek);
    tabThis.classList.toggle('active', !lastWeek);
    chk.checked = noIndian;

    var n = 0;
    for (var i = 0; i < cards.length; i++) {{
      var d = cards[i].dataset;
      if (d.week !== wk) continue;
      if (!showAll && d.mine === '0') continue;
      if (noIndian && d.indian === '1') continue;
      n++;
    }}
    countEl.textContent = n;
    datesEl.textContent = DATES[wk] || '';
    emptyEl.style.display = n ? 'none' : 'block';
  }}

  tabMine.addEventListener('click', function() {{ showAll = false; save('twr.all', false); apply(); }});
  tabAll.addEventListener('click', function() {{ showAll = true; save('twr.all', true); apply(); }});
  tabThis.addEventListener('click', function() {{ lastWeek = false; save('twr.last', false); apply(); }});
  tabLast.addEventListener('click', function() {{ lastWeek = true; save('twr.last', true); apply(); }});
  chk.addEventListener('change', function() {{ noIndian = chk.checked; save('twr.noin', noIndian); apply(); }});
  apply();

  // Filter menu. Deliberately NOT persisted — it always opens closed.
  function setMenu(open) {{
    menuEl.hidden = !open;
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }}
  menuBtn.addEventListener('click', function() {{ setMenu(menuEl.hidden); }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape' || e.keyCode === 27) setMenu(false);
  }});
  document.addEventListener('click', function(e) {{
    // Click-away close. The button's own handler already ran (it bubbles first),
    // so exempt it here or every open would immediately close itself.
    if (menuEl.hidden || menuEl.contains(e.target) || menuBtn.contains(e.target)) return;
    setMenu(false);
  }});

  // "+N" reveals that card's remaining providers. Delegated, so it costs one
  // listener rather than one per card.
  document.addEventListener('click', function(e) {{
    var t = e.target, more = t && t.closest ? t.closest('.pill.more') : null;
    if (more && more.parentNode) more.parentNode.classList.add('expanded');
  }});

  // Reformat the refresh time into the viewer's local timezone (falls back to
  // the embedded UTC text if JS is off or the date can't be parsed).
  var r = document.getElementById('refreshed');
  if (r) {{
    var d = new Date(r.getAttribute('data-ts'));
    if (!isNaN(d.getTime())) {{
      try {{
        r.textContent = d.toLocaleString(undefined, {{
          weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
        }});
      }} catch (e) {{}}
    }}
  }}
}})();
</script>
</body></html>"""


def resolved_cache_path(start: str, end: str, args) -> str:
    # v2: all_providers went from [str] to [{name, logo, mine}]. Bumping the key
    # retires pre-change caches instead of feeding the page the wrong shape.
    flags = f"v2-{args.max_age_days}-{int(args.movies_only)}-{int(args.tv_only)}"
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
    gm_movie, gm_tv = genre_maps()
    print()
    for it in candidates:
        iid = imdb_id_for(it["kind"], it["tmdb_id"])
        it["imdb_id"] = iid
        it["rating"], it["votes"] = imdb_ratings.get(iid, (None, 0)) if iid else (None, 0)
        mine, providers = watch_info(it["kind"], it["tmdb_id"])
        it["platforms"] = mine            # our services only — terminal list + Pushcut
        it["all_providers"] = providers   # every service, ours flagged — the card's pill row
        it["on_my_services"] = bool(mine)
        it["platform"] = "/".join(p["name"] for p in mine)           # string for terminal/push
        gmap = gm_movie if it["kind"] == "Movie" else gm_tv
        it["genres"] = [gmap[g] for g in it.get("genre_ids", []) if g in gmap]
        it["is_indian"] = is_indian(it)
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
    ap.add_argument("--notify", choices=["auto", "always", "never"], default="auto",
                    help="Pushcut policy: auto = only on Fridays; always = every run; never = off")
    ap.add_argument("--include-indian", action="store_true",
                    help="Include Indian titles in the terminal list and the Pushcut "
                         "notification (the HTML page always carries them, behind a toggle)")
    args = ap.parse_args()

    if not TMDB_KEY:
        print("ERROR: set the TMDB_API_KEY environment variable.", file=sys.stderr)
        return 1

    if args.start and args.end:
        start, end = args.start, args.end
        span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    else:
        mon, sun = week_window(dt.date.today())
        start, end = mon.isoformat(), sun.isoformat()
        span = 7
    # The preceding window of equal length — "last week" on the page, so a Monday
    # or Tuesday check-in can still see how the previous weekend's releases ranked.
    prev_end = dt.date.fromisoformat(start) - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=span - 1)

    print(f"Week window: {start} → {end}  (region {REGION})")
    print(f"Prior week:  {prev_start} → {prev_end}")
    print(f"Services: {', '.join(PROVIDERS)}\n")

    def load_week(w_start: str, w_end: str) -> list[dict]:
        """Resolve one window, honouring the --cache dev shortcut.

        The pipeline fetches fresh every run. --cache is a dev-only shortcut so we
        can iterate on HTML/formatting without re-hitting the APIs each time; the
        cache path is keyed by window, so the two weeks never collide.
        """
        cache_path = resolved_cache_path(w_start, w_end, args)
        if (args.cache and os.path.exists(cache_path)
                and (time.time() - os.path.getmtime(cache_path)) < RESOLVED_TTL_H * 3600):
            with open(cache_path, encoding="utf-8") as fh:
                found = json.load(fh)
            age_min = int((time.time() - os.path.getmtime(cache_path)) / 60)
            print(f"[--cache] Loaded {len(found)} releases for {w_start}→{w_end} "
                  f"from disk ({age_min}m old).\n")
            return found
        found = resolve_candidates(w_start, w_end, args)
        if args.cache:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(found, fh)
        return found

    candidates = load_week(start, end)
    # Last week is best-effort: it's a nice-to-have panel, so a TMDB hiccup there
    # must not take down the page for the week that actually matters.
    try:
        prev_candidates = load_week(prev_start.isoformat(), prev_end.isoformat())
    except Exception as e:
        print(f"WARNING: could not load last week ({e}); page will show this week only.",
              file=sys.stderr)
        prev_candidates = []

    if not candidates and not prev_candidates:
        print("\nNo brand-new movies or new series/seasons on your services this week.")
        return 0

    # Apply vote floor (drops unrated/too-new titles, which have 0 votes), BUT
    # never hide a brand-new premiere on YOUR services just because it has no IMDb
    # votes yet (e.g. Cape Fear, days old) — those are exactly what this surfaces.
    # The 0-vote exemption is scoped to on_my_services so the 'All' view (full of
    # global 0-vote premieres) stays clean.
    def passes_floor(it: dict) -> bool:
        return it["votes"] >= args.min_votes or (it["on_my_services"] and it["votes"] == 0)

    def ranked(pool: list[dict]) -> list[dict]:
        """Vote-floored and sorted best-first — the page's 'All' view for a window."""
        out = [it for it in pool if passes_floor(it)]
        out.sort(key=lambda x: (x["rating"] or 0, x["votes"]), reverse=True)
        return out

    # shown_all = everything this week (page's 'All' view, now the default);
    # shown = the subset on your subscriptions, which is what the terminal list and
    # the Pushcut notification report. Indian titles are dropped from those two
    # surfaces by default (matching the page's default-on toggle) — the page itself
    # still receives every title, since its toggle filters client-side.
    shown_all = ranked(candidates)
    prev_shown_all = ranked(prev_candidates)
    shown = [it for it in shown_all if it["on_my_services"]]
    if not args.include_indian:
        shown = [it for it in shown if not it.get("is_indian")]
    dropped = len(candidates) - len(shown_all)
    if dropped:
        print(f"(Hid {dropped} title(s) with fewer than {args.min_votes} IMDb votes.)\n")

    if not shown_all and not prev_shown_all:
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
    extra = len(shown_all) - len(shown)
    if extra:
        print(f"+{extra} more new this week not on your services — see the 'All' tab on the page.")
    if prev_shown_all:
        print(f"+{len(prev_shown_all)} from last week — see the 'Last Week' tab on the page.")

    # Write the HTML page (nice mobile layout — the real viewing surface).
    # Pass both weeks in full; the page tags every card with week / on-my-services /
    # is-Indian and the three toggles filter client-side.
    if args.html_out:
        now_utc = dt.datetime.now(dt.timezone.utc)
        generated = now_utc.strftime("%a, %b %-d %Y · %H:%M UTC")  # no-JS fallback
        generated_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")     # JS -> viewer's tz
        weeks = [
            {"key": "this", "label": "This Week", "start": start, "end": end,
             "items": shown_all},
            {"key": "last", "label": "Last Week", "start": prev_start.isoformat(),
             "end": prev_end.isoformat(), "items": prev_shown_all},
        ]
        html = generate_html(weeks, generated, generated_iso)
        d = os.path.dirname(args.html_out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.html_out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"Wrote {args.html_out} "
              f"({len(shown_all)} this week + {len(prev_shown_all)} last week).")

    # Decide whether to notify: auto = Fridays only, always = every run, never = off.
    # An empty list is never worth a buzz — with the vote floor, the my-services
    # filter and the Indian filter all applied, `shown` can be empty on a week the
    # page still has plenty on it.
    is_friday = dt.date.today().weekday() == 4
    should_notify = (bool(shown)
                     and (args.notify == "always" or (args.notify == "auto" and is_friday)))

    # Push to the phone via Pushcut, if configured and the policy allows.
    if PUSHCUT_WEBHOOK and not should_notify:
        if not shown:
            reason = "nothing new on your services this week"
        elif args.notify == "never":
            reason = "notifications disabled"
        else:
            reason = f"auto policy notifies Fridays only; today is {dt.date.today():%A}"
        print(f"Skipping Pushcut ({reason}).")
    if PUSHCUT_WEBHOOK and should_notify:
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
