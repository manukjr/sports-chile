#!/usr/bin/env python3
"""
sports.py — Chilean sports schedule scraper
Usage: python sports.py [YYYY-MM-DD]   (default: today CLT)
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
LOG = logging.getLogger("sports")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# Exact filter for Group 1.
# Key: (SofaScore tournament base name, SofaScore category name)
#   — base name = tournament name split on "," and stripped, so
#     "CONMEBOL Libertadores, Group A" → "CONMEBOL Libertadores"
# Value: (display name shown in HTML, broadcaster in Chile)
SOFASCORE_FOOTBALL_FILTER: dict[tuple[str, str], tuple[str, str]] = {
    ("UEFA Champions League",    "Europe"):        ("UEFA Champions League",       "ESPN / Disney+"),
    ("UEFA Europa League",       "Europe"):        ("UEFA Europa League",          "ESPN / Disney+"),
    ("Premier League",           "England"):       ("Premier League",              "ESPN / Disney+"),
    ("Serie A",                  "Italy"):         ("Serie A Italia",              "ESPN / Disney+"),
    ("Bundesliga",               "Germany"):       ("Bundesliga",                  "ESPN / Disney+"),
    ("LaLiga",                   "Spain"):         ("La Liga",                     "ESPN / Disney+"),
    ("Ligue 1",                  "France"):        ("Ligue 1",                     "ESPN / Disney+"),
    ("CONMEBOL Libertadores",    "South America"): ("Copa Libertadores",           "Disney+ Premium"),
    ("CONMEBOL Sudamericana",    "South America"): ("Copa Sudamericana",           "Disney+ Premium / DirecTV Sports"),
    ("Brasileirão Betano",       "Brazil"):        ("Brasileirao Betano",          "KICK.com"),
    ("Liga de Primera",          "Chile"):         ("Liga de Primera Chile",       "CDF"),
    ("Liga Profesional de Fútbol", "Argentina"):   ("Liga Profesional Argentina",  "ESPN / Disney+"),
}

BROADCASTER_MAP = {
    "NFL":              "ESPN / Disney+",
    "NBA":              "ESPN / Disney+",   # fallback only — overridden per game
    "NFL":              "ESPN / Disney+",   # fallback only — overridden per game
    "MLB":              "ESPN / Disney+",   # fallback only — overridden per game
    "UFC":              "Paramount+",
    "F1":               "ESPN / Disney+ Premium",
    "WEC":              "YouTube (FIA WEC oficial)",
    "GT World Challenge": "YouTube (SRO Motorsports oficial)",
    "ATP":              "ESPN / Disney+",
}

# Maps US broadcast network names (from ESPN API) to Chilean platforms.
# Warner Bros. Discovery holds NBA/LatAm rights for NBC/Peacock games
# (TNT lost US rights but WBD kept international). ESPN/ABC/CBS/FOX stay
# with ESPN LatAm. Amazon TNF is Amazon everywhere.
ESPN_NETWORK_TO_CHILE: dict[str, str] = {
    # Disney / ESPN group
    "ESPN":         "ESPN / Disney+",
    "ESPN2":        "ESPN / Disney+",
    "ESPNU":        "ESPN / Disney+",
    "ABC":          "ESPN / Disney+",
    # Warner / TNT group  (NBC & Peacock → WBD holds LatAm NBA deal)
    "NBC":          "TNT Sports / HBO Max",
    "Peacock":      "TNT Sports / HBO Max",
    "TNT":          "TNT Sports / HBO Max",
    "TBS":          "TNT Sports / HBO Max",
    "truTV":        "TNT Sports / HBO Max",
    # Amazon
    "Prime Video":  "Amazon Prime Video",
    "Amazon":       "Amazon Prime Video",
    # NFL-specific national nets → ESPN LatAm carries them
    "CBS":          "ESPN / Disney+",
    "FOX":          "ESPN / Disney+",
    "FS1":          "ESPN / Disney+",
    "NFL Network":  "ESPN / Disney+",
    # MLB national nets
    "MLB Network":  "ESPN / Disney+",
    "MLB.TV":       "MLB.TV",
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
# GROUP 1 — Fútbol Europeo & Sudamericano  (SofaScore)
# ---------------------------------------------------------------------------

async def fetch_group1(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    """Fetch football events from SofaScore public API."""
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date_str}"
    errors: list[str] = []
    events: list[Event] = []

    try:
        r = await client.get(url, headers={**HEADERS, "Referer": "https://www.sofascore.com/"})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("Group1 SofaScore error: %s", exc)
        errors.append(f"Fútbol (SofaScore): {exc}")
        return events, errors

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    for ev in data.get("events", []):
        tourn = ev.get("tournament", {})
        raw_name = tourn.get("name", "")
        category_name = tourn.get("category", {}).get("name", "")

        # Normalise: strip stage suffix ("UEFA Champions League, Knockout stage" → "UEFA Champions League")
        base_name = raw_name.split(",")[0].strip()

        key = (base_name, category_name)
        if key not in SOFASCORE_FOOTBALL_FILTER:
            continue

        display_comp, platform = SOFASCORE_FOOTBALL_FILTER[key]
        home = ev.get("homeTeam", {}).get("name", "TBD")
        away = ev.get("awayTeam", {}).get("name", "TBD")
        start_ts = ev.get("startTimestamp")

        # SofaScore groups events by matchday, so the response for date X can include
        # events whose actual kick-off (in CLT) falls on a different day. Drop those.
        if start_ts:
            event_date_clt = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(CLT).date()
            if event_date_clt != target_date:
                continue

        time_clt = _utc_to_clt(start_ts) if start_ts else "TBD"
        round_info = ev.get("roundInfo", {}).get("name", "")

        events.append(Event(
            competition=display_comp,
            category="soccer",
            home_team=home,
            away_team=away,
            time_clt=time_clt,
            platform=platform,
            round=round_info,
        ))

    return events, errors


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
        # ESPN event name is "Team A at Team B" or "Team A vs Team B"
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

        # Annotate with US network for transparency (e.g. "TNT Sports / HBO Max (NBC)")
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


async def _fetch_ufc(target_date: datetime.date, client: httpx.AsyncClient) -> tuple[list[Event], str | None]:
    """Fetch UFC fight card from ESPN's public MMA scoreboard API.
    Works from any IP (no geo-redirect, no bot protection)."""
    url = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
    try:
        r = await client.get(url, params={"dates": target_date.strftime("%Y%m%d")}, headers=HEADERS)
        if r.status_code in (400, 404):
            return [], None
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("UFC ESPN error: %s", exc)
        return [], f"UFC (ESPN): {exc}"

    events: list[Event] = []

    for ev in data.get("events", []):
        fights = ev.get("competitions", [])
        if not fights:
            continue

        # Infer card section from start time: latest time = main card, earlier = prelims.
        times = sorted(set(f.get("date", "") for f in fights if f.get("date")))
        if len(times) == 1:
            section_map = {times[0]: "Cartelera Estelar"}
        elif len(times) == 2:
            section_map = {times[0]: "Prelims", times[1]: "Cartelera Estelar"}
        else:
            section_map = {times[0]: "Early Prelims", times[-1]: "Cartelera Estelar"}
            for t in times[1:-1]:
                section_map[t] = "Prelims"

        for fight in fights:
            competitors = sorted(fight.get("competitors", []), key=lambda x: x.get("order", 0))
            fighter1 = competitors[0].get("athlete", {}).get("displayName", "TBD") if len(competitors) > 0 else "TBD"
            fighter2 = competitors[1].get("athlete", {}).get("displayName", "TBD") if len(competitors) > 1 else "TBD"
            weight   = fight.get("type", {}).get("abbreviation", "")
            fight_dt = fight.get("date", "")
            section  = section_map.get(fight_dt, "UFC")

            events.append(Event(
                competition="UFC",
                category="us-sports",
                home_team=fighter1,
                away_team=fighter2,
                time_clt=_iso_to_clt(fight_dt),
                platform=_platform("UFC"),
                round=f"{section} · {weight}" if weight else section,
            ))

    return events, None


async def fetch_group2(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    date_compact = date_str.replace("-", "")
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    nfl_url  = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={date_compact}"
    nba_url  = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date_compact}"

    results = await asyncio.gather(
        _espn_sport("NFL", nfl_url, "NFL", client),
        _espn_sport("NBA", nba_url, "NBA", client),
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

    # Each session key in the Jolpica response → Spanish display label.
    # SprintQualifying (pre-2023 name) and SprintShootout (2023+ name) are both included.
    SESSION_KEYS: list[tuple[str, str]] = [
        ("FirstPractice",    "Práctica 1"),
        ("SecondPractice",   "Práctica 2"),
        ("ThirdPractice",    "Práctica 3"),
        ("SprintShootout",   "Sprint Clasificación"),
        ("SprintQualifying", "Sprint Clasificación"),
        ("Sprint",           "Sprint"),
        ("Qualifying",       "Clasificación"),
        # Main race handled separately below
    ]

    def _session_time(date_s: str, time_s: str) -> str:
        """Convert Jolpica date + time strings to CLT 'HH:MM'."""
        if not time_s:
            return "TBD"
        try:
            dt = datetime.strptime(
                f"{date_s}T{time_s.rstrip('Z')}+00:00", "%Y-%m-%dT%H:%M:%S%z"
            )
            return dt.astimezone(CLT).strftime("%H:%M")
        except ValueError:
            return "TBD"

    races = data.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    for race in races:
        round_num = race.get("round", "")
        race_name = race.get("raceName", "Gran Premio")
        circuit   = race.get("Circuit", {}).get("circuitName", "")

        # Build the full list of (session_date, session_time_str, label)
        sessions: list[tuple[str, str, str]] = []
        for key, label in SESSION_KEYS:
            s = race.get(key)
            if s:
                sessions.append((s.get("date", ""), s.get("time", ""), label))
        # Main race
        sessions.append((race.get("date", ""), race.get("time", ""), "Carrera"))

        for s_date, s_time, label in sessions:
            if not s_date:
                continue
            try:
                session_date = datetime.strptime(s_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if session_date != target_date:
                continue  # only show sessions that actually fall on the target day

            events.append(Event(
                competition="F1",
                category="motor",
                home_team=race_name,
                away_team="",
                time_clt=_session_time(s_date, s_time),
                platform=_platform("F1"),
                round=f"Ronda {round_num} — {label} · {circuit}",
            ))

    return events, None


# ---------------------------------------------------------------------------
# 3-letter uppercase month abbreviations used by gt-world-challenge-europe.com
# ---------------------------------------------------------------------------
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
        """Extract a date from a calendar__date-start or __date-end div."""
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
            round=f"{start.strftime('%d %b')}–{end.strftime('%d %b %Y')}" if end and end != start else start.strftime("%d %b %Y"),
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

    # Step 1 — collect race URLs for the current year
    try:
        r = await client.get("https://www.fiawec.com", headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as exc:
        LOG.error("WEC homepage error: %s", exc)
        return events, f"WEC: {exc}"

    race_links = list(dict.fromkeys(  # preserve order, deduplicate
        f"https://www.fiawec.com{a['href']}"
        for a in soup.find_all("a", href=True)
        if _re.search(rf"/en/race/[^\"']+{year}", a["href"])
    ))
    if not race_links:
        return events, None

    # Step 2 — fetch all race pages in parallel
    async def _fetch_race_page(url: str) -> tuple[str, str]:
        try:
            r2 = await client.get(url, headers=HEADERS, follow_redirects=True)
            return url, r2.text if r2.status_code == 200 else ""
        except Exception:
            return url, ""

    pages = await asyncio.gather(*[_fetch_race_page(u) for u in race_links])

    # Step 3 — parse JSON-LD for startDate / endDate
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
                round=f"{start.strftime('%d %b')}–{end.strftime('%d %b %Y')}" if end != start else start.strftime("%d %b %Y"),
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
# GROUP 4 — ATP Tennis  (via SofaScore — same API as Group 1)
# ---------------------------------------------------------------------------

# SofaScore tournament category IDs for ATP events
_ATP_KEYWORDS = ["atp", "grand slam", "masters", "wimbledon", "roland garros", "us open", "australian open"]


async def fetch_group4(date_str: str, client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    """Fetch ATP tennis events from SofaScore public API."""
    url = f"https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/{date_str}"
    events: list[Event] = []
    errors: list[str] = []

    try:
        r = await client.get(url, headers={**HEADERS, "Referer": "https://www.sofascore.com/"})
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        LOG.error("Group4 SofaScore Tennis error: %s", exc)
        errors.append(f"ATP Tennis (SofaScore): {exc}")
        return events, errors

    seen_tournaments: set[str] = set()

    for ev in data.get("events", []):
        tourn = ev.get("tournament", {})
        tourn_name = tourn.get("name", "")
        category = tourn.get("category", {}).get("name", "")

        # Keep only ATP (men's) events, filter out WTA / ITF / doubles qualifying noise
        combined = f"{tourn_name} {category}".lower()
        if not any(kw in combined for kw in _ATP_KEYWORDS):
            continue
        # Skip doubles and qualifying rounds for readability
        if any(skip in tourn_name.lower() for skip in ["doubles", "qualifying", "qual."]):
            continue

        home = ev.get("homeTeam", {}).get("name", "")
        away = ev.get("awayTeam", {}).get("name", "")
        start_ts = ev.get("startTimestamp")
        time_clt = _utc_to_clt(start_ts) if start_ts else "TBD"
        round_info = ev.get("roundInfo", {}).get("name", "")

        # Deduplicate: same tournament shown once as summary if >5 matches
        if tourn_name not in seen_tournaments or len(events) < 20:
            seen_tournaments.add(tourn_name)
            events.append(Event(
                competition=f"ATP — {tourn_name}",
                category="other",
                home_team=home,
                away_team=away,
                time_clt=time_clt,
                platform=_platform("ATP"),
                round=round_info,
            ))

    return events, errors


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
DAYS_ES_ABBR = {
    0: "Lun", 1: "Mar", 2: "Mié", 3: "Jue",
    4: "Vie", 5: "Sáb", 6: "Dom",
}
MONTHS_ES_ABBR = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Ago",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def _date_es(d: datetime.date) -> str:
    day_name = DAYS_ES[d.weekday()]
    month_name = MONTHS_ES[d.month]
    return f"{day_name}, {d.day} de {month_name} de {d.year}"


def generate_html(
    events_by_date: dict[str, list[Event]],
    errors_by_date: dict[str, list[str]],
    today_str: str,
) -> str:
    """Generate a single HTML page covering a 7-day window with a date selector."""
    dates       = sorted(events_by_date.keys())
    today_date  = datetime.strptime(today_str, "%Y-%m-%d").date()
    total_today = len(events_by_date.get(today_str, []))

    # ── Date selector buttons ─────────────────────────────────────────────────
    date_btns_html = ""
    for ds in dates:
        d       = datetime.strptime(ds, "%Y-%m-%d").date()
        label   = f"{DAYS_ES_ABBR[d.weekday()]} {d.day} {MONTHS_ES_ABBR[d.month]}"
        date_es = _date_es(d)
        active  = " active" if ds == today_str else ""
        date_btns_html += (
            f'<button class="date-btn{active}" data-date="{ds}" data-date-es="{date_es}">'
            f"{label}</button>\n      "
        )

    # ── Table rows (all 7 days; JS controls visibility) ───────────────────────
    rows_html = ""
    for ds, events in events_by_date.items():
        for ev in events:
            c         = CATEGORY_COLORS.get(ev["category"], "#9b5de5")
            rnd       = f"<br><small style='color:#888'>{ev['round']}</small>" if ev.get("round") else ""
            comp_attr = ev["competition"].replace("&", "&amp;").replace('"', "&quot;")
            match_str = (
                f"{ev['home_team']} <span class='vs'>vs</span> {ev['away_team']}"
                if ev["away_team"] else ev["home_team"]
            )
            rows_html += f"""
        <tr data-date="{ds}" data-category="{ev['category']}" data-competition="{comp_attr}">
          <td class="bar-cell"><span class="bar" style="background:{c}"></span></td>
          <td class="comp">{ev['competition']}{rnd}</td>
          <td class="match">{match_str}</td>
          <td class="time">{ev['time_clt']}</td>
          <td class="platform">{ev['platform']}</td>
        </tr>"""

    # ── Per-date error sections ───────────────────────────────────────────────
    error_sections_html = ""
    for ds, errors in errors_by_date.items():
        if errors:
            errs_li = "".join(f"<li>{e}</li>" for e in errors)
            vis     = "" if ds == today_str else ' style="display:none"'
            error_sections_html += f"""
  <div class="errors" data-date="{ds}"{vis}>
    <strong>⚠ Fuentes con errores (datos parciales):</strong>
    <ul>{errs_li}</ul>
  </div>"""

    # ── Legend ────────────────────────────────────────────────────────────────
    labels = {"soccer": "Fútbol", "us-sports": "Deportes USA", "motor": "Motor", "other": "Otros"}
    legend_html = ""
    for cat, col in CATEGORY_COLORS.items():
        legend_html += f'<span class="legend-item"><span class="dot" style="background:{col}"></span>{labels[cat]}</span>'

    today_date_es = _date_es(today_date)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sports Schedule: Chile — {today_str}</title>
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
  /* ── Date selector ──────────────────────────────────────── */
  .date-selector {{
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 1.5rem;
  }}
  .date-btn {{
    background: #0e0f18;
    border: 1px solid #1e1f2e;
    color: #666;
    border-radius: 8px;
    padding: 0.45rem 0.85rem;
    font-size: 0.8rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }}
  .date-btn:hover {{ border-color: #444; color: #aaa; }}
  .date-btn.active {{
    background: #00b4d8;
    border-color: #00b4d8;
    color: #000;
    font-weight: 700;
  }}
  /* ── Filter panel ──────────────────────────────────────── */
  .filter-panel {{
    background: #0e0f18;
    border: 1px solid #1e1f2e;
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.5rem;
  }}
  .filter-top {{
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.85rem;
  }}
  .filter-label {{
    font-size: 0.75rem;
    font-weight: 600;
    color: #666;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    flex: 1;
  }}
  .filter-btn {{
    font-size: 0.72rem;
    background: none;
    border: 1px solid #2a2b3a;
    color: #888;
    border-radius: 5px;
    padding: 0.2rem 0.65rem;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }}
  .filter-btn:hover {{ border-color: #555; color: #ccc; }}
  .filter-groups {{ display: flex; gap: 2rem; flex-wrap: wrap; }}
  .filter-group {{ min-width: 170px; }}
  .filter-group-head {{
    display: flex;
    align-items: center;
    margin-bottom: 0.45rem;
  }}
  .filter-group-head label {{
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    user-select: none;
  }}
  .filter-comp-list {{
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    padding-left: 1.25rem;
    border-left: 2px solid #1e1f2e;
    margin-left: 0.4rem;
  }}
  .filter-comp-list label {{
    font-size: 0.76rem;
    color: #777;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    user-select: none;
    transition: color 0.15s;
  }}
  .filter-comp-list label:hover {{ color: #bbb; }}
  input[type="checkbox"] {{ accent-color: #00b4d8; cursor: pointer; }}
  /* ── Table ─────────────────────────────────────────────── */
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
    <div class="date-str" id="date-display">{today_date_es}</div>
  </header>
  <div class="meta">
    <div class="count"><strong id="event-count">{total_today}</strong> evento{"s" if total_today != 1 else ""}</div>
    <div class="legend">{legend_html}</div>
  </div>
  <div class="date-selector">
      {date_btns_html}
  </div>
  <div class="filter-panel">
    <div class="filter-top">
      <span class="filter-label">🔍 Filtrar eventos</span>
      <button class="filter-btn" id="btn-all">Todo</button>
      <button class="filter-btn" id="btn-none">Ninguno</button>
    </div>
    <div class="filter-groups" id="filter-groups"></div>
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
      {rows_html}
      <tr id="filter-empty" style="display:none">
        <td colspan="5" style="text-align:center;color:#666;padding:2rem">No hay eventos para mostrar.</td>
      </tr>
    </tbody>
  </table>
  {error_sections_html}
  <footer>Generado el {datetime.now(CLT).strftime("%Y-%m-%d %H:%M")} CLT · Ventana: {dates[0]} → {dates[-1]}</footer>
</div>
<script>
(function () {{
  const CAT_META = {{
    'soccer':    {{ label: '⚽ Fútbol',        color: '#00b4d8' }},
    'us-sports': {{ label: '🏀 Deportes USA',  color: '#f77f00' }},
    'motor':     {{ label: '🏎️ Motor',          color: '#e63946' }},
    'other':     {{ label: '🎾 Otros',           color: '#9b5de5' }},
  }};

  let activeDate = '{today_str}';
  const allRows  = Array.from(document.querySelectorAll('tbody tr[data-category]'));

  // Build/rebuild the competition filter for the currently active date
  function buildFilter() {{
    const dayRows = allRows.filter(r => r.dataset.date === activeDate);
    const catMap  = {{}};
    dayRows.forEach(r => {{
      const cat = r.dataset.category, comp = r.dataset.competition;
      if (!catMap[cat]) catMap[cat] = new Set();
      catMap[cat].add(comp);
    }});
    const container = document.getElementById('filter-groups');
    container.innerHTML = '';
    Object.entries(CAT_META).forEach(([cat, meta]) => {{
      if (!catMap[cat]) return;
      const comps = [...catMap[cat]].sort();
      const g = document.createElement('div');
      g.className = 'filter-group';
      g.innerHTML =
        '<div class="filter-group-head"><label>' +
        '<input type="checkbox" class="cat-cb" data-cat="' + cat + '" checked> ' +
        '<span style="color:' + meta.color + '">' + meta.label + '</span>' +
        '</label></div><div class="filter-comp-list"></div>';
      const list = g.querySelector('.filter-comp-list');
      comps.forEach(comp => {{
        const lbl = document.createElement('label');
        const cb  = document.createElement('input');
        cb.type = 'checkbox'; cb.className = 'comp-cb';
        cb.dataset.cat = cat; cb.dataset.comp = comp; cb.checked = true;
        lbl.appendChild(cb);
        lbl.appendChild(document.createTextNode(' ' + comp));
        list.appendChild(lbl);
      }});
      container.appendChild(g);
    }});
  }}

  // Show rows matching active date + checked competitions; update counter
  function applyFilter() {{
    const checked = new Set();
    document.querySelectorAll('.comp-cb:checked').forEach(cb => {{
      checked.add(cb.dataset.cat + '|||' + cb.dataset.comp);
    }});
    let visible = 0;
    allRows.forEach(r => {{
      const show = r.dataset.date === activeDate &&
                   checked.has(r.dataset.category + '|||' + r.dataset.competition);
      r.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    const ce = document.getElementById('event-count');
    if (ce) ce.textContent = visible;
    const fe = document.getElementById('filter-empty');
    if (fe) fe.style.display = (visible === 0) ? '' : 'none';
  }}

  // Sync category master checkbox state
  function syncCat(cat) {{
    const cbs   = document.querySelectorAll('.comp-cb[data-cat="' + cat + '"]');
    const n     = Array.from(cbs).filter(c => c.checked).length;
    const catCb = document.querySelector('.cat-cb[data-cat="' + cat + '"]');
    if (!catCb) return;
    catCb.checked = n > 0;
    catCb.indeterminate = n > 0 && n < cbs.length;
  }}

  // Date button clicks
  document.querySelectorAll('.date-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeDate = btn.dataset.date;
      const dd = document.getElementById('date-display');
      if (dd) dd.textContent = btn.dataset.dateEs;
      document.querySelectorAll('.errors[data-date]').forEach(el => {{
        el.style.display = el.dataset.date === activeDate ? '' : 'none';
      }});
      buildFilter();
      applyFilter();
    }});
  }});

  // Checkbox event delegation
  document.addEventListener('change', e => {{
    const t = e.target;
    if (t.classList.contains('cat-cb')) {{
      document.querySelectorAll('.comp-cb[data-cat="' + t.dataset.cat + '"]')
        .forEach(cb => {{ cb.checked = t.checked; }});
    }} else if (t.classList.contains('comp-cb')) {{
      syncCat(t.dataset.cat);
    }}
    applyFilter();
  }});

  // "Todo" / "Ninguno" buttons
  document.getElementById('btn-all').addEventListener('click', () => {{
    document.querySelectorAll('.cat-cb,.comp-cb').forEach(cb => {{ cb.checked = true; cb.indeterminate = false; }});
    applyFilter();
  }});
  document.getElementById('btn-none').addEventListener('click', () => {{
    document.querySelectorAll('.cat-cb,.comp-cb').forEach(cb => {{ cb.checked = false; cb.indeterminate = false; }});
    applyFilter();
  }});

  // Initialise on page load
  buildFilter();
  applyFilter();
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pick_date() -> datetime.date:
    """Return the start date for the 7-day window.
    CLI arg (YYYY-MM-DD) takes priority; otherwise today in CLT."""
    today = datetime.now(CLT).date()
    if len(sys.argv) > 1:
        try:
            return datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"Formato inválido: '{sys.argv[1]}'. Usa YYYY-MM-DD.")
            sys.exit(1)
    return today


async def main() -> None:
    today     = _pick_date()
    today_str = today.strftime("%Y-%m-%d")
    dates     = [today + timedelta(days=i) for i in range(7)]

    LOG.info("Fetching 7-day window: %s → %s (CLT)", dates[0], dates[-1])

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # SofaScore requires a session handshake — visit homepage first
        try:
            await client.get("https://www.sofascore.com/", headers=HEADERS, follow_redirects=True)
        except Exception:
            pass  # non-fatal

        # Fetch all 4 groups for all 7 days concurrently (28 tasks total)
        day_tasks = [
            asyncio.gather(
                fetch_group1(d.strftime("%Y-%m-%d"), client),
                fetch_group2(d.strftime("%Y-%m-%d"), client),
                fetch_group3(d.strftime("%Y-%m-%d"), client),
                fetch_group4(d.strftime("%Y-%m-%d"), client),
                return_exceptions=True,
            )
            for d in dates
        ]
        all_day_results = await asyncio.gather(*day_tasks)

    events_by_date: dict[str, list[Event]] = {}
    errors_by_date: dict[str, list[str]]   = {}

    def sort_key(ev: Event) -> str:
        t = ev["time_clt"]
        return "99:99" if t in ("TBD", "??:??") else t

    for day, day_results in zip(dates, all_day_results):
        ds: str = day.strftime("%Y-%m-%d")
        day_events: list[Event] = []
        day_errors: list[str]   = []
        for i, result in enumerate(day_results, 1):
            if isinstance(result, Exception):
                LOG.error("Day %s group %d crashed: %s", ds, i, result)
                day_errors.append(f"Grupo {i}: {result}")
            else:
                evs, errs = result
                day_events.extend(evs)
                day_errors.extend(errs)
        day_events.sort(key=sort_key)
        events_by_date[ds] = day_events
        errors_by_date[ds] = day_errors
        LOG.info("  %s: %d events, %d errors", ds, len(day_events), len(day_errors))

    html     = generate_html(events_by_date, errors_by_date, today_str)
    out_path = Path(f"deportes_{today_str}.html")
    out_path.write_text(html, encoding="utf-8")
    LOG.info("Saved → %s", out_path.resolve())

    webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    asyncio.run(main())
