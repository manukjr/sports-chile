#!/usr/bin/env python3
"""
sports_ci.py — CI-friendly sports schedule scraper (GitHub Actions / headless).

Replaces SofaScore (blocked on datacenter IPs) with ESPN's public APIs:
  • Group 1  Football → ESPN Soccer scoreboard API   (no key)
  • Group 4  Tennis   → ESPN Tennis scoreboard API   (no key, ATP + WTA)

No external API keys required — everything runs on ESPN public endpoints
and public scraping (UFC, F1, WEC, GT World Challenge).

Usage (normally called via run_ci.py, not directly):
    python sports_ci.py [YYYY-MM-DD]
"""

import asyncio
import logging
import sys
import webbrowser
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# America/Santiago handles both CLT (UTC-4, winter) and CLST (UTC-3, summer)
# automatically — never hardcode the offset.
CLT = ZoneInfo("America/Santiago")
LOG = logging.getLogger("sports_ci")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# ── ESPN Soccer API (Group 1) ─────────────────────────────────────────────────
# Same public API family as NBA/NFL/MLB (Group 2). No key required.
# slug → (display name, Chilean platform)
ESPN_SOCCER_LEAGUES: dict[str, tuple[str, str]] = {
    "uefa.champions":        ("UEFA Champions League",      "ESPN / Disney+"),
    "uefa.europa":           ("UEFA Europa League",         "ESPN / Disney+"),
    "eng.1":                 ("Premier League",             "ESPN / Disney+"),
    "ita.1":                 ("Serie A Italia",             "ESPN / Disney+"),
    "ger.1":                 ("Bundesliga",                 "ESPN / Disney+"),
    "esp.1":                 ("La Liga",                    "ESPN / Disney+"),
    "fra.1":                 ("Ligue 1",                    "ESPN / Disney+"),
    "conmebol.libertadores": ("Copa Libertadores",          "Disney+ Premium"),
    "conmebol.sudamericana": ("Copa Sudamericana",          "Disney+ Premium / DirecTV Sports"),
    "bra.1":                 ("Brasileirao Betano",         "KICK.com"),
    "arg.1":                 ("Liga Profesional Argentina", "ESPN / Disney+"),
    "chi.1":                 ("Liga de Primera Chile",      "CDF"),
}


# ── ESPN Tennis API (Group 4) ─────────────────────────────────────────────────
# Same public API family as football (Group 1) and US sports (Group 2). No key.
ESPN_TENNIS_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis"

# ── Broadcaster maps ──────────────────────────────────────────────────────────
BROADCASTER_MAP = {
    "NFL":               "ESPN / Disney+",
    "NBA":               "ESPN / Disney+",   # fallback — overridden per game
    "MLB":               "ESPN / Disney+",   # fallback — overridden per game
    "UFC":               "Paramount+",
    "F1":                "ESPN / Disney+ Premium",
    "WEC":               "YouTube (FIA WEC oficial)",
    "GT World Challenge": "YouTube (SRO Motorsports oficial)",
    "ATP":               "ESPN / Disney+",
    "WTA":               "ESPN / Disney+",
}

# Maps US broadcast network names (from ESPN API) to Chilean platforms.
# Warner Bros. Discovery holds NBA/LatAm rights for NBC/Peacock games.
ESPN_NETWORK_TO_CHILE: dict[str, str] = {
    # Disney / ESPN group
    "ESPN":        "ESPN / Disney+",
    "ESPN2":       "ESPN / Disney+",
    "ESPNU":       "ESPN / Disney+",
    "ABC":         "ESPN / Disney+",
    # Warner / TNT group  (NBC & Peacock → WBD holds LatAm NBA deal)
    "NBC":         "TNT Sports / HBO Max",
    "Peacock":     "TNT Sports / HBO Max",
    "TNT":         "TNT Sports / HBO Max",
    "TBS":         "TNT Sports / HBO Max",
    "truTV":       "TNT Sports / HBO Max",
    # Amazon
    "Prime Video": "Amazon Prime Video",
    "Amazon":      "Amazon Prime Video",
    # NFL-specific national nets → ESPN LatAm carries them
    "CBS":         "ESPN / Disney+",
    "FOX":         "ESPN / Disney+",
    "FS1":         "ESPN / Disney+",
    "NFL Network": "ESPN / Disney+",
    # MLB national nets
    "MLB Network": "ESPN / Disney+",
    "MLB.TV":      "MLB.TV",
}

CATEGORY_COLORS = {
    "soccer":    "#00b4d8",
    "us-sports": "#f77f00",
    "motor":     "#e63946",
    "other":     "#9b5de5",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Event(TypedDict):
    competition: str
    category: str          # soccer | us-sports | motor | other
    home_team: str
    away_team: str
    time_clt: str          # "HH:MM"
    platform: str
    round: str


def _platform(competition: str) -> str:
    for key, val in BROADCASTER_MAP.items():
        if key.lower() in competition.lower():
            return val
    return "—"


def _utc_to_clt(ts: int) -> str:
    """Unix timestamp → 'HH:MM' in CLT."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(CLT)
    return dt.strftime("%H:%M")


def _iso_to_clt(iso: str) -> str:
    """ISO-8601 string → 'HH:MM' in CLT. Falls back to '??:??'."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CLT)
        return dt.strftime("%H:%M")
    except Exception:
        return "??:??"


# ---------------------------------------------------------------------------
# GROUP 1 — Fútbol Europeo & Sudamericano  (ESPN Soccer public API)
# ---------------------------------------------------------------------------

async def _espn_soccer(
    slug: str,
    display_name: str,
    platform: str,
    date_compact: str,
    client: httpx.AsyncClient,
) -> tuple[list[Event], str | None]:
    """Fetch one league's fixtures from ESPN's public soccer scoreboard API."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
    try:
        r = await client.get(url, params={"dates": date_compact}, headers=HEADERS)
        # ESPN returns 400/404 for leagues with no coverage — treat as empty, not error
        if r.status_code in (400, 404):
            return [], None
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("ESPN soccer %s error: %s", slug, exc)
        return [], f"{display_name} (ESPN soccer): {exc}"

    events: list[Event] = []
    for game in data.get("events", []):
        date_iso = game.get("date", "")
        time_clt = _iso_to_clt(date_iso)
        status   = game.get("status", {}).get("type", {}).get("description", "")

        # Extract home/away from the competitors array (more reliable than splitting name)
        home_t = away_t = ""
        us_networks: list[str] = []
        for comp in game.get("competitions", []):
            for competitor in comp.get("competitors", []):
                team_name = competitor.get("team", {}).get("displayName", "")
                if competitor.get("homeAway") == "home":
                    home_t = team_name
                elif competitor.get("homeAway") == "away":
                    away_t = team_name
            for bc in comp.get("broadcasts", []):
                us_networks.extend(bc.get("names", []))

        # Resolve US broadcast network → Chilean platform (same logic as Group 2)
        resolved_platform = platform
        for net in us_networks:
            mapped = ESPN_NETWORK_TO_CHILE.get(net)
            if mapped:
                resolved_platform = mapped
                break

        # Round info lives inside league node
        round_info = (
            game.get("season", {}).get("slug", "")
            or status
        )

        events.append(Event(
            competition=display_name,
            category="soccer",
            home_team=home_t or "TBD",
            away_team=away_t or "TBD",
            time_clt=time_clt,
            platform=resolved_platform,
            round=round_info,
        ))

    return events, None


async def fetch_group1(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    """Fetch football fixtures from ESPN's public soccer API for all configured leagues."""
    date_compact = date_str.replace("-", "")
    tasks = [
        _espn_soccer(slug, name, platform, date_compact, client)
        for slug, (name, platform) in ESPN_SOCCER_LEAGUES.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    all_events: list[Event] = []
    errors: list[str] = []
    for evs, err in results:
        all_events.extend(evs)
        if err:
            errors.append(err)

    return all_events, errors


# ---------------------------------------------------------------------------
# GROUP 2 — Deportes USA  (ESPN + UFC)
# ---------------------------------------------------------------------------

async def _espn_sport(
    sport_key: str,
    url: str,
    competition: str,
    client: httpx.AsyncClient,
) -> tuple[list[Event], str | None]:
    """Generic ESPN scoreboard fetcher."""
    events: list[Event] = []
    try:
        r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("ESPN %s error: %s", sport_key, exc)
        return events, f"{competition} (ESPN): {exc}"

    for game in data.get("events", []):
        name = game.get("name", "")
        if " at " in name:
            away_t, home_t = name.split(" at ", 1)
        elif " vs " in name:
            away_t, home_t = name.split(" vs ", 1)
        else:
            away_t, home_t = name, ""

        date_iso = game.get("date", "")
        time_clt = _iso_to_clt(date_iso)
        status = game.get("status", {}).get("type", {}).get("description", "")

        # Extract US broadcast network(s) and resolve to Chilean platform
        us_networks: list[str] = []
        for comp in game.get("competitions", []):
            for bc in comp.get("broadcasts", []):
                us_networks.extend(bc.get("names", []))

        platform = _platform(competition)   # default fallback
        for net in us_networks:
            mapped = ESPN_NETWORK_TO_CHILE.get(net)
            if mapped:
                platform = mapped
                break   # use the first recognised network

        # Annotate with US network for transparency
        if us_networks:
            known = [n for n in us_networks if n in ESPN_NETWORK_TO_CHILE]
            if known:
                platform = f"{platform} · {'/'.join(known)} US"

        events.append(Event(
            competition=competition,
            category="us-sports",
            home_team=home_t.strip(),
            away_team=away_t.strip(),
            time_clt=time_clt,
            platform=platform,
            round=status,
        ))

    return events, None


def _ufc_fighter_name(fight_el, corner: str) -> str:
    """Extract a clean fighter name from a .c-listing-fight element.
    corner is 'red' or 'blue'. Handles the given+family span split."""
    el = fight_el.select_one(f".c-listing-fight__corner-name--{corner}")
    if not el:
        return "?"
    given  = el.select_one(".c-listing-fight__corner-given-name")
    family = el.select_one(".c-listing-fight__corner-family-name")
    if given and family:
        return f"{given.get_text(strip=True)} {family.get_text(strip=True)}"
    return el.get_text(strip=True)


async def _fetch_ufc(target_date: datetime.date, client: httpx.AsyncClient) -> tuple[list[Event], str | None]:
    """Scrape UFC events listing, then each matching event's detail page for all fights."""
    events: list[Event] = []
    try:
        r = await client.get("https://www.ufc.com/events", headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as exc:
        LOG.error("UFC scrape error: %s", exc)
        return events, f"UFC (ufc.com): {exc}"

    # Step 1: find cards matching target_date via Unix timestamp
    for card in soup.select("article.c-card-event--result"):
        date_el = card.select_one(".c-card-event--result__date.tz-change-data")
        if not date_el:
            continue
        ts_str = date_el.get("data-main-card-timestamp", "")
        try:
            main_ts = int(ts_str)
        except (ValueError, TypeError):
            continue

        event_date_clt = datetime.fromtimestamp(main_ts, tz=timezone.utc).astimezone(CLT).date()
        if event_date_clt != target_date:
            continue

        pre_ts_str = date_el.get("data-prelims-card-timestamp", "")
        try:
            prelims_ts = int(pre_ts_str)
        except (ValueError, TypeError):
            prelims_ts = None

        # Step 2: get the event detail URL
        link_el = card.select_one("h3.c-card-event--result__headline a")
        href = link_el.get("href", "") if link_el else ""
        if not href:
            continue
        detail_url = f"https://www.ufcespanol.com{href}" if href.startswith("/") else href

        # Step 3: fetch detail page and parse all fights
        try:
            r2 = await client.get(detail_url, headers=HEADERS, follow_redirects=True)
            r2.raise_for_status()
            soup2 = BeautifulSoup(r2.text, "lxml")
        except Exception as exc:
            LOG.error("UFC detail page error: %s", exc)
            continue

        fight_triples: list[tuple] = []

        # Strategy A: explicit div wrappers (most detailed events)
        sections_a = [
            ("div.main-card",                "Cartelera Estelar", main_ts),
            ("div.fight-card-prelims",       "Prelims",           prelims_ts),
            ("div.fight-card-prelims-early", "Early Prelims",     prelims_ts),
        ]
        for sel, label, ts in sections_a:
            sec = soup2.select_one(sel)
            if sec:
                for f in sec.select(".c-listing-fight"):
                    fight_triples.append((f, label, ts))

        # Strategy B: h3 section headers as dividers
        if not fight_triples:
            SECTION_LABELS = {
                "Main Card":    ("Cartelera Estelar", main_ts),
                "Prelims":      ("Prelims",           prelims_ts),
                "Early Prelims":("Early Prelims",     prelims_ts),
            }
            current_label, current_ts = "Cartelera Estelar", main_ts
            for el in soup2.find_all(["h3", "article"]):
                if el.name == "h3" and el.get_text(strip=True) in SECTION_LABELS:
                    current_label, current_ts = SECTION_LABELS[el.get_text(strip=True)]
                elif el.name == "article" and "c-listing-fight" in " ".join(el.get("class", [])):
                    fight_triples.append((el, current_label, current_ts))

        # Strategy C: all fights on page, no section info
        if not fight_triples:
            for f in soup2.select(".c-listing-fight"):
                fight_triples.append((f, "Cartelera Estelar", main_ts))

        for fight, label, ts in fight_triples:
            red  = _ufc_fighter_name(fight, "red")
            blue = _ufc_fighter_name(fight, "blue")
            if red == "?" and blue == "?":
                continue
            wc_el = fight.select_one(".c-listing-fight__class-text")
            wc = wc_el.get_text(strip=True) if wc_el else ""
            time_clt = _utc_to_clt(ts) if ts else "TBD"
            events.append(Event(
                competition="UFC",
                category="us-sports",
                home_team=red,
                away_team=blue,
                time_clt=time_clt,
                platform=_platform("UFC"),
                round=f"{label} · {wc}" if wc else label,
            ))

    return events, None


async def fetch_group2(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    date_compact = date_str.replace("-", "")
    target_date  = datetime.strptime(date_str, "%Y-%m-%d").date()

    nfl_url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={date_compact}"
    nba_url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date_compact}"
    mlb_url = f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={date_compact}"

    results = await asyncio.gather(
        _espn_sport("NFL", nfl_url, "NFL", client),
        _espn_sport("NBA", nba_url, "NBA", client),
        _espn_sport("MLB", mlb_url, "MLB", client),
        _fetch_ufc(target_date, client),
        return_exceptions=False,
    )

    all_events: list[Event] = []
    errors: list[str] = []
    for evs, err in results:
        all_events.extend(evs)
        if err:
            errors.append(err)

    return all_events, errors


# ---------------------------------------------------------------------------
# GROUP 3 — Motor (F1 / WEC / GT World Challenge)
# ---------------------------------------------------------------------------

async def _fetch_f1(year: int, target_date: datetime.date, client: httpx.AsyncClient) -> tuple[list[Event], str | None]:
    # Jolpica is the community-maintained Ergast successor
    url = f"https://api.jolpi.ca/ergast/f1/{year}.json"
    events: list[Event] = []
    try:
        r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("F1 Jolpica error: %s", exc)
        return events, f"F1 (Jolpica): {exc}"

    races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    for race in races:
        race_date = race.get("date", "")
        try:
            rd = datetime.strptime(race_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Include race day and ±1 day (practice/quali)
        if abs((rd - target_date).days) > 1:
            continue

        round_num = race.get("round", "")
        race_name = race.get("raceName", "Gran Premio")
        circuit   = race.get("Circuit", {}).get("circuitName", "")
        time_str  = race.get("time", "")
        if time_str:
            dummy_dt = datetime.strptime(
                f"{race_date}T{time_str.rstrip('Z')}+00:00", "%Y-%m-%dT%H:%M:%S%z"
            )
            time_clt = dummy_dt.astimezone(CLT).strftime("%H:%M")
        else:
            time_clt = "TBD"

        label = (
            "Carrera" if rd == target_date
            else "Clasificación" if (rd - target_date).days == 1
            else "Práctica"
        )
        events.append(Event(
            competition="F1",
            category="motor",
            home_team=race_name,
            away_team=circuit,
            time_clt=time_clt,
            platform=_platform("F1"),
            round=f"Ronda {round_num} — {label}",
        ))

    return events, None


# 3-letter uppercase month abbreviations used by gt-world-challenge-europe.com
_GT_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


async def _fetch_gt_world_challenge(
    target_date: datetime.date, client: httpx.AsyncClient
) -> tuple[list[Event], str | None]:
    """Parse li.calendar__list-item entries on the GT World Challenge calendar."""
    url = "https://www.gt-world-challenge-europe.com/calendar"
    events: list[Event] = []
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as exc:
        LOG.error("GT World Challenge scrape error: %s", exc)
        return events, f"GT World Challenge: {exc}"

    def _parse_gt_date(container) -> date | None:
        day_el   = container.select_one(".calendar__date-number")
        month_el = container.select_one(".calendar__date-month")
        year_el  = container.select_one(".calendar__date-year")
        if not (day_el and month_el and year_el):
            return None
        try:
            day   = int(day_el.get_text(strip=True))
            month = _GT_MONTH_ABBR.get(month_el.get_text(strip=True).upper(), 0)
            year  = int(year_el.get_text(strip=True))
            return datetime(year, month, day).date() if month else None
        except (ValueError, TypeError):
            return None

    for item in soup.select("li.calendar__list-item"):
        start_el = item.select_one("div.calendar__date-start")
        end_el   = item.select_one("div.calendar__date-end")
        if not start_el:
            continue
        start = _parse_gt_date(start_el)
        end   = _parse_gt_date(end_el) if end_el else start
        if not start:
            continue
        if not (start <= target_date <= (end or start)):
            continue

        name_el = item.select_one("h3.calendar__race-header")
        name    = name_el.get_text(strip=True) if name_el else "GT World Challenge"

        events.append(Event(
            competition="GT World Challenge",
            category="motor",
            home_team=name,
            away_team="",
            time_clt="TBD",
            platform=_platform("GT World Challenge"),
            round=(
                f"{start.strftime('%d %b')}–{end.strftime('%d %b %Y')}"
                if end and end != start else start.strftime("%d %b %Y")
            ),
        ))

    return events, None


async def _fetch_wec(
    target_date: datetime.date, client: httpx.AsyncClient
) -> tuple[list[Event], str | None]:
    """
    1. Scrape fiawec.com homepage for /en/race/...-{year} links.
    2. Fetch each race page in parallel and parse JSON-LD for startDate/endDate.
    3. Return events whose date window covers target_date.
    """
    import json as _json
    import re as _re
    events: list[Event] = []
    year = target_date.year

    try:
        r = await client.get("https://www.fiawec.com", headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as exc:
        LOG.error("WEC homepage error: %s", exc)
        return events, f"WEC: {exc}"

    race_links = list(dict.fromkeys(
        f"https://www.fiawec.com{a['href']}"
        for a in soup.find_all("a", href=True)
        if _re.search(rf"/en/race/[^\"']+{year}", a["href"])
    ))
    if not race_links:
        return events, None

    async def _fetch_race_page(url: str) -> tuple[str, str]:
        try:
            r2 = await client.get(url, headers=HEADERS, follow_redirects=True)
            return url, r2.text if r2.status_code == 200 else ""
        except Exception:
            return url, ""

    pages = await asyncio.gather(*[_fetch_race_page(u) for u in race_links])

    for url, html in pages:
        if not html:
            continue
        soup2 = BeautifulSoup(html, "lxml")
        for script in soup2.find_all("script", type="application/ld+json"):
            try:
                ld = _json.loads(script.string or "")
            except Exception:
                continue
            start_raw = ld.get("startDate", "")
            end_raw   = ld.get("endDate",   "")
            if not start_raw:
                continue
            try:
                start = datetime.fromisoformat(start_raw).date()
                end   = datetime.fromisoformat(end_raw).date() if end_raw else start
            except ValueError:
                continue
            if not (start <= target_date <= end):
                continue

            name = ld.get("name") or soup2.select_one("h1, h2")
            if hasattr(name, "get_text"):
                name = name.get_text(strip=True)
            name = str(name or "WEC Event")

            events.append(Event(
                competition="WEC",
                category="motor",
                home_team=name,
                away_team="",
                time_clt="TBD",
                platform=_platform("WEC"),
                round=(
                    f"{start.strftime('%d %b')}–{end.strftime('%d %b %Y')}"
                    if end != start else start.strftime("%d %b %Y")
                ),
            ))
            break  # one entry per race page

    return events, None


async def fetch_group3(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    year = target_date.year

    results = await asyncio.gather(
        _fetch_f1(year, target_date, client),
        _fetch_wec(target_date, client),
        _fetch_gt_world_challenge(target_date, client),
        return_exceptions=False,
    )

    all_events: list[Event] = []
    errors: list[str] = []
    for evs, err in results:
        all_events.extend(evs)
        if err:
            errors.append(err)

    return all_events, errors


# ---------------------------------------------------------------------------
# GROUP 4 — ATP & WTA Tennis  (ESPN public tennis scoreboard API)
# ---------------------------------------------------------------------------

async def _fetch_espn_tennis(
    tour_slug: str,    # "atp" or "wta"
    label: str,        # "ATP" or "WTA"
    singles_key: str,  # "Men's Singles" or "Women's Singles"
    date_str: str,
    client: httpx.AsyncClient,
) -> tuple[list[Event], str | None]:
    """
    Fetch tennis matches for one tour from ESPN's public scoreboard API.

    ESPN returns one 'event' per tournament (e.g. Roland Garros, Hamburg Open).
    Each event contains 'groupings' split by gender/category.
    We filter to the correct singles grouping and to matches on target_date only.

    Grand Slams: shown as one summary row (100+ matches/day — individual listing is noise).
    Other tournaments: individual match rows, capped at 10 per tournament.
    """
    url = f"{ESPN_TENNIS_BASE}/{tour_slug}/scoreboard"
    try:
        r = await client.get(url, params={"dates": date_str.replace("-", "")}, headers=HEADERS)
        if r.status_code in (400, 404):
            return [], None
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("ESPN tennis %s error: %s", label, exc)
        return [], f"Tennis {label} (ESPN): {exc}"

    platform = _platform(label)
    events: list[Event] = []

    for ev in data.get("events", []):
        tourn_name = ev.get("name", "")
        is_major   = ev.get("major", False)

        for grp in ev.get("groupings", []):
            grp_name = grp.get("grouping", {}).get("displayName", "")
            if grp_name != singles_key:
                continue  # skip doubles, mixed, opposite gender

            # Keep only matches that start today and are not yet Final
            today_comps = [
                c for c in grp.get("competitions", [])
                if date_str in c.get("date", "")
                and c.get("status", {}).get("type", {}).get("description", "") != "Final"
            ]
            if not today_comps:
                continue

            if is_major:
                # Grand Slam: one summary row to avoid flooding the page
                first_time = min((c.get("date", "") for c in today_comps), default="")
                events.append(Event(
                    competition=f"{label} — {tourn_name}",
                    category="other",
                    home_team=f"{len(today_comps)} partidos programados",
                    away_team="",
                    time_clt=_iso_to_clt(first_time) if first_time else "TBD",
                    platform=platform,
                    round=grp_name,
                ))
            else:
                # Regular tournament: individual match rows (cap at 10)
                for c in today_comps[:10]:
                    competitors = c.get("competitors", [])
                    home = next(
                        (x.get("athlete", {}).get("displayName", "?")
                         for x in competitors if x.get("homeAway") == "home"), "TBD"
                    )
                    away = next(
                        (x.get("athlete", {}).get("displayName", "?")
                         for x in competitors if x.get("homeAway") == "away"), "TBD"
                    )
                    court = c.get("venue", {}).get("court", "")
                    events.append(Event(
                        competition=f"{label} — {tourn_name}",
                        category="other",
                        home_team=home,
                        away_team=away,
                        time_clt=_iso_to_clt(c.get("date", "")),
                        platform=platform,
                        round=court,
                    ))

    return events, None


async def fetch_group4(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    """Fetch ATP and WTA tennis from ESPN's public tennis scoreboard API. No key required."""
    results = await asyncio.gather(
        _fetch_espn_tennis("atp", "ATP", "Men's Singles",   date_str, client),
        _fetch_espn_tennis("wta", "WTA", "Women's Singles", date_str, client),
        return_exceptions=False,
    )
    all_events: list[Event] = []
    errors: list[str] = []
    for evs, err in results:
        all_events.extend(evs)
        if err:
            errors.append(err)
    return all_events, errors


# ---------------------------------------------------------------------------
# HTML Generator
# ---------------------------------------------------------------------------

MONTHS_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}
DAYS_ES = {
    0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
    4: "viernes", 5: "sábado", 6: "domingo",
}


def _date_es(d: datetime.date) -> str:
    day_name   = DAYS_ES[d.weekday()]
    month_name = MONTHS_ES[d.month]
    return f"{day_name}, {d.day} de {month_name} de {d.year}"


def generate_html(events: list[Event], date_str: str, errors: list[str]) -> str:
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    date_es = _date_es(target_date)
    total   = len(events)

    def color(ev: Event) -> str:
        return CATEGORY_COLORS.get(ev["category"], "#9b5de5")

    def match_display(ev: Event) -> str:
        if ev["away_team"]:
            return f"{ev['home_team']} <span class='vs'>vs</span> {ev['away_team']}"
        return ev["home_team"]

    rows_html = ""
    for ev in events:
        c   = color(ev)
        rnd = f"<br><small style='color:#888'>{ev['round']}</small>" if ev.get("round") else ""
        rows_html += f"""
        <tr>
          <td class="bar-cell"><span class="bar" style="background:{c}"></span></td>
          <td class="comp">{ev['competition']}{rnd}</td>
          <td class="match">{match_display(ev)}</td>
          <td class="time">{ev['time_clt']}</td>
          <td class="platform">{ev['platform']}</td>
        </tr>"""

    error_section = ""
    if errors:
        errs_li = "".join(f"<li>{e}</li>" for e in errors)
        error_section = f"""
        <div class="errors">
          <strong>⚠ Fuentes con errores (datos parciales):</strong>
          <ul>{errs_li}</ul>
        </div>"""

    legend_html = ""
    labels = {"soccer": "Fútbol", "us-sports": "Deportes USA", "motor": "Motor", "other": "Otros"}
    for cat, col in CATEGORY_COLORS.items():
        legend_html += f'<span class="legend-item"><span class="dot" style="background:{col}"></span>{labels[cat]}</span>'

    empty_msg = ""
    if total == 0:
        empty_msg = '<tr><td colspan="5" style="text-align:center;color:#666;padding:2rem">No se encontraron eventos para esta fecha.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sports Schedule: Chile — {date_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #08090f;
    color: #e0e0e0;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    padding: 2rem 1rem;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  header {{ margin-bottom: 2rem; }}
  header h1 {{
    font-size: 1.8rem;
    font-weight: 700;
    color: #fff;
    letter-spacing: -0.02em;
  }}
  header .date-str {{
    font-size: 1rem;
    color: #888;
    margin-top: 0.25rem;
    text-transform: capitalize;
  }}
  .meta {{
    display: flex;
    align-items: center;
    gap: 1.5rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }}
  .count {{
    background: #1a1b26;
    border: 1px solid #2a2b3a;
    border-radius: 8px;
    padding: 0.4rem 0.9rem;
    font-size: 0.85rem;
    color: #aaa;
  }}
  .count strong {{ color: #fff; }}
  .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .legend-item {{
    display: flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.8rem;
    color: #888;
  }}
  .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.92rem;
  }}
  thead th {{
    background: #0e0f18;
    color: #666;
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 0.75rem 0.75rem;
    text-align: left;
    border-bottom: 1px solid #1e1f2e;
  }}
  thead th:first-child {{ width: 6px; padding: 0; }}
  tbody tr {{
    border-bottom: 1px solid #111320;
    transition: background 0.15s;
  }}
  tbody tr:hover {{ background: #0e0f1a; }}
  .bar-cell {{ width: 6px; padding: 0; }}
  .bar {{
    display: block;
    width: 4px;
    height: 100%;
    min-height: 2.8rem;
    border-radius: 0 2px 2px 0;
  }}
  td {{ padding: 0.7rem 0.75rem; vertical-align: middle; }}
  td.comp {{ color: #aaa; font-size: 0.82rem; min-width: 160px; }}
  td.match {{ color: #eee; font-weight: 500; }}
  .vs {{ color: #555; font-weight: 400; font-size: 0.85em; margin: 0 0.3em; }}
  td.time {{
    color: #00b4d8;
    font-weight: 700;
    font-size: 1rem;
    white-space: nowrap;
    min-width: 70px;
  }}
  td.platform {{ color: #888; font-size: 0.82rem; white-space: nowrap; }}
  .errors {{
    margin-top: 2rem;
    background: #1a1010;
    border: 1px solid #3a1515;
    border-radius: 8px;
    padding: 1rem 1.25rem;
    font-size: 0.82rem;
    color: #cc6666;
  }}
  .errors ul {{ margin-top: 0.5rem; padding-left: 1.25rem; }}
  .errors li {{ margin-top: 0.25rem; }}
  footer {{
    margin-top: 2rem;
    font-size: 0.75rem;
    color: #333;
    text-align: center;
  }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🏟 Sports Schedule: Chile</h1>
    <div class="date-str">{date_es}</div>
  </header>
  <div class="meta">
    <div class="count"><strong>{total}</strong> evento{"s" if total != 1 else ""}</div>
    <div class="legend">{legend_html}</div>
  </div>
  <table>
    <thead>
      <tr>
        <th></th>
        <th>Competición</th>
        <th>Partido</th>
        <th>Hora CLT</th>
        <th>Dónde ver</th>
      </tr>
    </thead>
    <tbody>
      {rows_html or empty_msg}
    </tbody>
  </table>
  {error_section}
  <footer>Generado el {datetime.now(CLT).strftime("%Y-%m-%d %H:%M")} CLT</footer>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pick_date() -> datetime.date:
    """Interactive date selector. CLI arg takes priority; otherwise prompt."""
    today = datetime.now(CLT).date()

    if len(sys.argv) > 1:
        try:
            return datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Formato inválido: '{sys.argv[1]}'. Usa YYYY-MM-DD.")
            sys.exit(1)

    yesterday = today - timedelta(days=1)
    tomorrow  = today + timedelta(days=1)

    options = [
        ("1", f"Hoy          ({today})"),
        ("2", f"Ayer         ({yesterday})"),
        ("3", f"Mañana       ({tomorrow})"),
        ("4", "Otra fecha   (escribe YYYY-MM-DD)"),
    ]

    print("\n  ╔══════════════════════════════════════╗")
    print(  "  ║   🏟  Sports Chile — elige fecha     ║")
    print(  "  ╠══════════════════════════════════════╣")
    for key, label in options:
        print(f"  ║  [{key}] {label:32s}║")
    print(  "  ╚══════════════════════════════════════╝")

    while True:
        raw = input("\n  Opción [1]: ").strip() or "1"

        if raw == "1":
            return today
        elif raw == "2":
            return yesterday
        elif raw == "3":
            return tomorrow
        elif raw == "4":
            while True:
                d = input("  Fecha (YYYY-MM-DD): ").strip()
                try:
                    return datetime.strptime(d, "%Y-%m-%d").date()
                except ValueError:
                    print("  Formato inválido, intenta de nuevo.")
        else:
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                print("  Opción no válida. Elige 1, 2, 3 o 4.")


async def main() -> None:
    target   = _pick_date()
    date_str = target.strftime("%Y-%m-%d")
    LOG.info("Fetching sports for %s (CLT)", date_str)

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            fetch_group1(date_str, client),
            fetch_group2(date_str, client),
            fetch_group3(date_str, client),
            fetch_group4(date_str, client),
            return_exceptions=True,
        )

    all_events: list[Event] = []
    all_errors: list[str]   = []

    for i, result in enumerate(results, 1):
        if isinstance(result, Exception):
            LOG.error("Group %d crashed unexpectedly: %s", i, result)
            all_errors.append(f"Grupo {i}: {result}")
        else:
            evs, errs = result
            all_events.extend(evs)
            all_errors.extend(errs)

    # Sort by CLT time; put TBD / ??:?? at the end
    def sort_key(ev: Event) -> str:
        t = ev["time_clt"]
        return "99:99" if t in ("TBD", "??:??") else t

    all_events.sort(key=sort_key)

    LOG.info("Total events: %d (errors: %d)", len(all_events), len(all_errors))

    html      = generate_html(all_events, date_str, all_errors)
    out_path  = Path(f"deportes_{date_str}.html")
    out_path.write_text(html, encoding="utf-8")
    LOG.info("Saved → %s", out_path.resolve())

    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    asyncio.run(main())
