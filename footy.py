#!/usr/bin/env python3
"""footy - what's worth watching today.

Ranks today's football matches by how interesting they are, using ESPN's
public JSON API. No API key, no signup, no ads, no cookies, no trackers.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

API = "https://site.api.espn.com/apis"
# Some networks/proxies filter on User-Agent, and which value passes varies by
# environment, so try a short chain rather than betting on one.
USER_AGENTS = (None, "curl/8.9.1", "Mozilla/5.0 (compatible; footy/1.0)")
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("FOOTY_CONFIG", ROOT / "config.toml"))
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "footy"
TIMEOUT = 20
__version__ = "1.1.0"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "leagues": {
        "eng.1": {"name": "Premier League", "short": "PL", "weight": 1.00},
        "eng.2": {"name": "Championship", "short": "Champ", "weight": 0.72},
        "esp.1": {"name": "LaLiga", "short": "LaLiga", "weight": 0.97},
        "ita.1": {"name": "Serie A", "short": "Serie A", "weight": 0.95},
        "ger.1": {"name": "Bundesliga", "short": "Bundesliga", "weight": 0.93},
        "ger.2": {"name": "2. Bundesliga", "short": "2.BL", "weight": 0.76},
        "fra.1": {"name": "Ligue 1", "short": "Ligue 1", "weight": 0.85},
        "ned.1": {"name": "Eredivisie", "short": "Eredivisie", "weight": 0.82},
        "uefa.champions": {"name": "Champions League", "short": "UCL", "weight": 1.15},
        "uefa.europa": {"name": "Europa League", "short": "UEL", "weight": 0.90},
    },
    "favourites": [],
    "display": {"limit": 0, "min_stars": 0, "notify_top": 3},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if not CONFIG_PATH.exists():
        return cfg
    if tomllib is None:
        warn(f"Python 3.11+ needed to read {CONFIG_PATH}; using defaults.")
        return cfg
    try:
        user = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        warn(f"could not parse {CONFIG_PATH}: {exc}; using defaults.")
        return cfg

    if isinstance(user.get("leagues"), dict) and user["leagues"]:
        cfg["leagues"] = {
            slug: {
                "name": meta.get("name", slug),
                "short": meta.get("short", meta.get("name", slug)),
                "weight": float(meta.get("weight", 1.0)),
            }
            for slug, meta in user["leagues"].items()
            if isinstance(meta, dict)
        }
    if isinstance(user.get("favourites"), list):
        cfg["favourites"] = [str(t) for t in user["favourites"]]
    if isinstance(user.get("display"), dict):
        cfg["display"].update(user["display"])
    if isinstance(user.get("iptv"), dict) and user["iptv"]:
        cfg["iptv"] = user["iptv"]
    if "iptv" in cfg and CONFIG_PATH.stat().st_mode & 0o077:
        warn(f"{CONFIG_PATH} is readable by other users but holds IPTV "
             f"credentials; run `chmod 600 {CONFIG_PATH}`")
    return cfg


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def warn(msg: str) -> None:
    print(f"footy: {msg}", file=sys.stderr)


@lru_cache(maxsize=4096)
def normalise(text: str) -> str:
    """Fold accents/punctuation so 'Atlético' matches 'atletico'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def fetch(url: str, cache_key: str | None = None, ttl: int = 3600, text: bool = False):
    """GET a URL, optionally memoised on disk for `ttl` seconds.

    JSON by default; pass `text=True` for a raw string (e.g. XMLTV EPG).
    """
    suffix = ".txt" if text else ".json"
    cache_file = CACHE_DIR / f"{cache_key}{suffix}" if cache_key else None
    if cache_file and cache_file.exists():
        if time.time() - cache_file.stat().st_mtime < ttl:
            try:
                body = cache_file.read_text(encoding="utf-8")
                return body if text else json.loads(body)
            except Exception:
                pass  # corrupt cache, refetch

    data, last_exc = None, None
    for ua in USER_AGENTS:
        req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                data = raw if text else json.loads(raw)
            break
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (403, 429):
                break  # only a UA/rate block is worth retrying
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            break

    if data is None:
        if cache_file and cache_file.exists():  # serve stale rather than nothing
            try:
                body = cache_file.read_text(encoding="utf-8")
                return body if text else json.loads(body)
            except Exception:
                pass
        raise RuntimeError(f"{last_exc}")

    if cache_file:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(data if text else json.dumps(data), encoding="utf-8")
        except OSError:
            pass
    return data


def find_entries(obj):
    """ESPN nests standings entries at varying depths; find the first list."""
    if isinstance(obj, dict):
        if "entries" in obj and isinstance(obj["entries"], list):
            return obj["entries"]
        for value in obj.values():
            found = find_entries(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_entries(value)
            if found:
                return found
    return None


# --------------------------------------------------------------------------
# data fetching
# --------------------------------------------------------------------------

def is_cross_border(slug: str) -> bool:
    """True for continental competitions, whose tables mix clubs from many leagues."""
    return slug.startswith(("uefa.", "conmebol.", "concacaf.", "fifa."))


# Within one run the same day/league is asked for repeatedly (the -f prefetch,
# then the scoring pass). The disk cache still costs a read and a JSON parse
# each time, so keep the parsed result in memory too.
_FIXTURES: dict[tuple[str, str], list] = {}
_TABLES: dict[tuple[str, int], dict] = {}
_TEAMS: dict[str, list[dict]] = {}


def get_fixtures(slug: str, day: date) -> list[dict]:
    key = (slug, f"{day:%Y%m%d}")
    if key not in _FIXTURES:
        # Today's scores move minute to minute; a fixture three weeks out does
        # not, so don't re-fetch the whole lookahead window every five minutes.
        ttl = 300 if day <= date.today() else 12 * 3600
        url = f"{API}/site/v2/sports/soccer/{slug}/scoreboard?dates={day:%Y%m%d}"
        _FIXTURES[key] = fetch(
            url, cache_key=f"fx_{slug}_{day:%Y%m%d}", ttl=ttl
        ).get("events", []) or []
    return _FIXTURES[key]


def _roster(slug: str) -> list[dict]:
    """Every team dict in a competition this season, memoised in memory."""
    if slug in _TEAMS:
        return _TEAMS[slug]
    url = f"{API}/site/v2/sports/soccer/{slug}/teams"
    try:
        data = fetch(url, cache_key=f"tm_{slug}", ttl=24 * 3600)
        teams = [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]
    except (RuntimeError, KeyError, IndexError):
        teams = []
    _TEAMS[slug] = teams
    return teams


def get_league_teams(slug: str) -> set[str]:
    """Team ids taking part in a competition this season."""
    return {t["id"] for t in _roster(slug)}


def leagues_with(cfg, team_ids) -> list[str]:
    """Which tracked competitions those teams actually appear in."""
    slugs = list(cfg["leagues"])
    with ThreadPoolExecutor(max_workers=min(10, len(slugs))) as pool:
        rosters = dict(zip(slugs, pool.map(get_league_teams, slugs)))
    hits = [s for s in slugs if team_ids & rosters[s]]
    return hits or slugs  # unknown team: fall back to scanning everything


def resolve_team_ids(cfg, names) -> set[str]:
    """Turn configured names into team ids, using league rosters not fixtures."""
    if not names:
        return set()
    slugs = list(cfg["leagues"])
    with ThreadPoolExecutor(max_workers=min(10, len(slugs))) as pool:
        raw = list(pool.map(_roster_teams, slugs))
    teams = [t for group in raw for t in group]
    fake = [{"competitions": [{"competitors": [{"team": t} for t in teams]}]}]
    return resolve_favourites(names, fake)


def _roster_teams(slug):
    return _roster(slug)


def get_standings(slug: str, day: date) -> dict[str, dict]:
    """Map team id -> {rank, points, total} for the league table."""
    season = day.year if day.month >= 7 else day.year - 1
    if (slug, season) in _TABLES:
        return _TABLES[(slug, season)]
    url = f"{API}/v2/sports/soccer/{slug}/standings?season={season}"
    try:
        entries = find_entries(fetch(url, cache_key=f"st_{slug}_{season}", ttl=6 * 3600))
    except RuntimeError:
        _TABLES[(slug, season)] = {}
        return {}
    if not entries:
        _TABLES[(slug, season)] = {}
        return {}

    table: dict[str, dict] = {}
    for entry in entries:
        stats = {s.get("name"): s for s in entry.get("stats", [])}
        rank = stats.get("rank", {}).get("value")
        if rank is None:
            continue
        table[entry["team"]["id"]] = {
            "rank": int(rank),
            "name": entry["team"].get("shortDisplayName") or entry["team"]["displayName"],
            "points": stats.get("points", {}).get("value") or 0,
            "played": stats.get("gamesPlayed", {}).get("value") or 0,
            "gf": stats.get("pointsFor", {}).get("value") or 0,
            "ga": stats.get("pointsAgainst", {}).get("value") or 0,
            "won": stats.get("wins", {}).get("value") or 0,
            "drawn": stats.get("ties", {}).get("value") or 0,
            "lost": stats.get("losses", {}).get("value") or 0,
            "gd": stats.get("pointDifferential", {}).get("value") or 0,
            "total": len(entries),
        }
    _TABLES[(slug, season)] = table
    return table


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

# Fixtures that carry their own weight regardless of league position.
# Team IDs verified against ESPN's /teams endpoint.
DERBIES = {
    frozenset({"359", "367"}): "North London derby",
    frozenset({"364", "368"}): "Merseyside derby",
    frozenset({"364", "360"}): "Northwest derby",
    frozenset({"382", "360"}): "Manchester derby",
    frozenset({"363", "359"}): "London derby",
    frozenset({"363", "367"}): "London derby",
    frozenset({"83", "86"}): "El Clasico",
    frozenset({"86", "1068"}): "Madrid derby",
    frozenset({"110", "103"}): "Derby della Madonnina",
    frozenset({"110", "111"}): "Derby d'Italia",
    frozenset({"104", "112"}): "Derby della Capitale",
    frozenset({"114", "111"}): "Napoli v Juventus",
    frozenset({"132", "124"}): "Der Klassiker",
    frozenset({"160", "176"}): "Le Classique",
}


# A fixture's likely goal count, from how many goals each side is involved in.
# 2.0 total goals a game is dour, 4.0 is a shootout.
GOALS_DULL, GOALS_WILD = 2.0, 4.0


def goal_appeal(rows) -> float | None:
    """0..1 rating of how open a fixture is likely to be, or None if unknown."""
    rates = []
    for row in rows:
        played = row.get("played") or 0 if row else 0
        if not played:
            continue
        rates.append((row.get("gf", 0) + row.get("ga", 0)) / played)
    if not rates:
        return None
    rate = sum(rates) / len(rates)
    return max(0.0, min(1.0, (rate - GOALS_DULL) / (GOALS_WILD - GOALS_DULL)))


def form_wins(form: str | None) -> int:
    return (form or "").upper().count("W")


def score_match(event, table, league, fav_ids, domestic=None, cross=False):
    """Blend competition, table position, form, rivalry and allegiance."""
    comp = event["competitions"][0]
    competitors = comp.get("competitors", [])
    if len(competitors) != 2:
        return None

    teams = {}
    for c in competitors:
        teams[c.get("homeAway", "home")] = c
    home, away = teams.get("home"), teams.get("away")
    if not home or not away:
        return None

    hid, aid = home["team"]["id"], away["team"]["id"]

    if cross:
        # In a 36-team league phase, "27th" says nothing about how big a club
        # Real Madrid is. Judge European ties by domestic standing instead;
        # a side outside the tracked leagues is treated as a smaller club.
        domestic = domestic or {}
        hrow, arow = domestic.get(hid), domestic.get(aid)
        outsiders = (hrow is None) + (arow is None)
    else:
        hrow, arow = table.get(hid), table.get(aid)
        outsiders = 0

    # --- table quality: how high up are both sides, on a 0..1 scale
    if hrow and arow and hrow["total"] > 1:
        n = hrow["total"]
        qh = 1 - (hrow["rank"] - 1) / (n - 1)
        qa = 1 - (arow["rank"] - 1) / (n - 1)
        qh_raw, qa_raw = qh, qa
        quality = (qh + qa) / 2
        # Two games in, the table says almost nothing. Fade the signal toward
        # neutral until the season has enough matches behind it.
        played = min(hrow.get("played", 0), arow.get("played", 0))
        confidence = min(1.0, played / 8) if played else 0.0
        quality = 0.5 + (quality - 0.5) * confidence
        qh = 0.5 + (qh - 0.5) * confidence
        qa = 0.5 + (qa - 0.5) * confidence
        both_strong = 0.15 * confidence if (qh > 0.7 and qa > 0.7) else 0.0
        # six-pointer: close together and both meaningfully placed
        gap = abs(hrow["rank"] - arow["rank"])
        closeness = 0.12 * confidence * max(0.0, 1 - gap / 5) if quality > 0.45 else 0.0
    elif cross and (hrow or arow):
        # one side tracked, the other not: rate the known side, discount the other
        known = hrow or arow
        n = known["total"]
        qk = 1 - (known["rank"] - 1) / (n - 1) if n > 1 else 0.5
        played = known.get("played", 0)
        confidence = min(1.0, played / 8) if played else 0.0
        qk = 0.5 + (qk - 0.5) * confidence
        quality = (qk + OUTSIDER_QUALITY) / 2
        both_strong, closeness = 0.0, 0.0
    else:
        # no table at all (early cup rounds, season not started) - stay neutral
        quality, both_strong, closeness, confidence = 0.5, 0.0, 0.0, 0.0
        if cross and outsiders == 2:
            quality = OUTSIDER_QUALITY

    # --- form: recent wins for both sides
    hw, aw = form_wins(home.get("form")), form_wins(away.get("form"))
    form = (hw + aw) / 10

    # --- who is expected to win? Table position, form, and a nod to home
    # advantage. Early in a season the bar for calling it is deliberately
    # higher, because the table has not earned that confidence yet.
    edge = None
    if hrow and arow:
        sh = 0.70 * qh_raw + 0.30 * (hw / 5) + HOME_EDGE
        sa = 0.70 * qa_raw + 0.30 * (aw / 5)
        band = EVEN_BAND + 0.15 * (1 - confidence)
        diff = sh - sa
        edge = "even" if abs(diff) < band else ("home" if diff > 0 else "away")

    # --- rivalry
    derby = DERBIES.get(frozenset({hid, aid}))

    # Early in a season the table is thin but recent form still carries real
    # information, so shift weight from one to the other as confidence drops.
    conf = confidence if (hrow and arow) else 0.0
    form_weight = 0.18 + 0.30 * (1 - conf)

    # How entertaining the fixture looks, independent of how grand it is.
    goals = goal_appeal([hrow, arow])
    goals_term = 0.20 * goals * conf if goals is not None else 0.0

    raw = (
        0.42 * quality
        + both_strong
        + closeness
        + form_weight * form
        + goals_term
        + (0.22 if derby else 0.0)
    )
    score = raw * float(league.get("weight", 1.0))

    fav = [c["team"]["displayName"] for c in (home, away) if c["team"]["id"] in fav_ids]
    if fav:
        score += 0.30  # weighted boost, not a pin: a dull match stays dull

    return {
        "score": score,
        "edge": edge,
        "derby": derby,
        "favourite": bool(fav),
        "home": home,
        "away": away,
        "home_row": hrow,
        "away_row": arow,
        "event": event,
        "league": league,
    }


# Contractions people actually type, which no ESPN field spells out.
CONTRACTIONS = {
    "utd": "united", "st": "saint", "atl": "atletico",
    "gladbach": "monchengladbach", "dep": "deportivo",
}

TEAM_FIELDS = (
    "displayName", "shortDisplayName", "name", "abbreviation", "location",
    "nickname",
)


def expand(query: str) -> str:
    """Turn 'man utd' into 'man united' so it can match a real name."""
    return " ".join(CONTRACTIONS.get(tok, tok) for tok in query.split())


def resolve_favourites(names, all_events) -> set[str]:
    """Match configured favourite names against the teams actually playing.

    Matching runs one way only: the name you typed must appear inside the
    club's, never the reverse. The reverse let a three-letter abbreviation
    match anything containing it, so Chelsea ("CHE") answered to
    "Manchester United".
    """
    if not names:
        return set()

    teams: dict[str, tuple[str, set[str]]] = {}
    for event in all_events:
        for comp in event["competitions"][0].get("competitors", []):
            team = comp["team"]
            fields = {
                normalise(team[k]) for k in TEAM_FIELDS
                if team.get(k) and normalise(team[k])
            }
            teams[team["id"]] = (team.get("displayName", team["id"]), fields)

    ids: set[str] = set()
    for raw in names:
        want = expand(normalise(raw))
        if not want:
            continue

        exact = {tid for tid, (_, fields) in teams.items() if want in fields}
        hits = exact or {
            tid for tid, (_, fields) in teams.items()
            if any(want in field for field in fields)
        }

        if len(hits) > 1:
            found = ", ".join(sorted(teams[t][0] for t in hits))
            warn(f"favourite {raw!r} is ambiguous - matched {found}")
        ids |= hits
    return ids


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
YELLOW, CYAN, GREEN, RED, MAGENTA = (
    "\033[33m", "\033[36m", "\033[32m", "\033[31m", "\033[35m",
)


def colour_depth(force: bool = False) -> int:
    """24 for truecolor (kitty, wezterm...), 8 for 256, 4 for basic, 0 for none."""
    if os.environ.get("NO_COLOR") is not None:
        return 0
    if not force and not sys.stdout.isatty():
        return 0
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 24
    term = os.environ.get("TERM", "")
    if "256" in term or "kitty" in term or "direct" in term:
        return 8
    return 4 if term and term != "dumb" else 0


DEPTH = colour_depth()


def fg(rgb, c256, basic):
    """Pick the best available escape for a colour."""
    if DEPTH >= 24:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if DEPTH >= 8:
        return f"\033[38;5;{c256}m"
    return basic


# Who the table and form expect to win. Distinct hues, readable on light or
# dark terminal backgrounds.
FAV_C = DOG_C = EVEN_C = ""


def init_colours(force: bool = False) -> bool:
    """Resolve the palette once we know whether we're writing to a terminal."""
    global DEPTH, FAV_C, DOG_C, EVEN_C
    DEPTH = colour_depth(force)
    FAV_C = fg((111, 191, 143), 114, GREEN)    # the expected winner
    DOG_C = fg((224, 150, 74), 179, YELLOW)    # the underdog
    EVEN_C = fg((176, 143, 217), 140, MAGENTA)  # too close to call
    return DEPTH > 0


def supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# Absolute score thresholds, so five stars means "genuinely a big match",
# not merely "the best of a poor Tuesday".
STAR_CUTS = (0.38, 0.48, 0.58, 0.70)

# Assumed standing for a club outside the tracked domestic leagues.
OUTSIDER_QUALITY = 0.30

# Playing at home is worth a little; below this gap a match is a coin toss.
HOME_EDGE, EVEN_BAND = 0.04, 0.10


def stars(score: float) -> int:
    """Absolute 1..5 rating; a thin day genuinely looks thin."""
    return 1 + sum(score >= cut for cut in STAR_CUTS)


def match_state(m) -> str:
    """'pre', 'in' or 'post'."""
    return m["event"]["competitions"][0]["status"]["type"].get("state", "pre")


def kickoff(m, tz):
    return datetime.fromisoformat(
        m["event"]["date"].replace("Z", "+00:00")
    ).astimezone(tz)


def side_text(comp, row, mark) -> str:
    """Team name, league position, and whether they're home or away."""
    pos = f" {row['rank']}" if row else ""
    return f"{comp['team']['shortDisplayName']}{pos} ({mark})"


def edge_colour(edge, which):
    """Colour a side by whether the table and form expect it to win."""
    if edge is None:
        return None
    if edge == "even":
        return EVEN_C
    return FAV_C if edge == which else DOG_C


def render(matches, tz, colour: bool, show_date: date) -> str:
    if not matches:
        return "No matches in your leagues today."

    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    out = []

    width = max(
        max(len(side_text(m["home"], m["home_row"], "H")) for m in matches), 14
    )

    for m in matches:
        ev, comp = m["event"], m["event"]["competitions"][0]
        kick = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(tz)
        n = stars(m["score"])
        bar = C(YELLOW, "*" * n) + C(DIM, "." * (5 - n))

        status = comp["status"]["type"]
        state = status.get("state")
        if state == "pre":
            when = kick.strftime("%H:%M")
        elif state == "in":
            when = C(GREEN, status.get("displayClock", "LIVE").rjust(5))
        else:
            when = C(DIM, "FT   ")

        hplain = side_text(m["home"], m["home_row"], "H")
        aplain = side_text(m["away"], m["away_row"], "A")
        hcol, acol = edge_colour(m["edge"], "home"), edge_colour(m["edge"], "away")
        hside = " " * max(0, width - len(hplain)) + (
            C(hcol, hplain) if hcol else hplain
        )
        aside = C(acol, aplain) if acol else aplain

        if state == "pre":
            fixture = f"{hside} v {aside}"
        else:
            hs = m["home"].get("score", "0")
            as_ = m["away"].get("score", "0")
            fixture = f"{hside} {C(BOLD, f'{hs}-{as_}')} {aside}"

        tag = C(CYAN, m["league"]["short"])
        heart = C(RED, " <3") if m["favourite"] else ""
        out.append(f"  {bar}  {when}  {fixture}  {tag}{heart}")

        # second line: why this match is here
        notes = []
        if m["derby"]:
            notes.append(C(MAGENTA, m["derby"]))
        hf, af = m["home"].get("form"), m["away"].get("form")
        if hf and af:
            notes.append(f"{hf} / {af}")
        if notes:
            pad = " " * 16
            out.append(f"{pad}{C(DIM, '  '.join(notes))}")

    header = C(BOLD, show_date.strftime("%A %-d %B"))
    return f"\n  {header}\n\n" + "\n".join(out) + "\n"


def render_grouped(matches, tz, colour, show_date, per_league):
    """One block per competition, so smaller leagues are not buried."""
    if not matches:
        return "No matches in your leagues today."

    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    groups: dict[str, list] = {}
    for m in matches:
        groups.setdefault(m["league"]["name"], []).append(m)

    # strongest competition of the day first
    order = sorted(groups, key=lambda k: -groups[k][0]["score"])

    out = [f"\n  {C(BOLD, show_date.strftime('%A %-d %B'))}\n"]
    for name in order:
        picks = groups[name][:per_league]
        out.append(f"  {C(CYAN, name)}")
        for m in picks:
            ev = m["event"]
            kick = datetime.fromisoformat(
                ev["date"].replace("Z", "+00:00")
            ).astimezone(tz)
            n = stars(m["score"])
            bar = C(YELLOW, "*" * n) + C(DIM, "." * (5 - n))
            state = ev["competitions"][0]["status"]["type"].get("state")
            when = kick.strftime("%H:%M") if state == "pre" else (
                C(GREEN, "LIVE") if state == "in" else C(DIM, "FT  ")
            )
            heart = C(RED, " <3") if m["favourite"] else ""

            hc, ac = edge_colour(m["edge"], "home"), edge_colour(m["edge"], "away")
            hn = side_text(m["home"], m["home_row"], "H")
            an = side_text(m["away"], m["away_row"], "A")
            hn, an = (C(hc, hn) if hc else hn), (C(ac, an) if ac else an)
            out.append(f"    {bar}  {when}  {hn} v {an}{heart}")
        out.append("")
    return "\n".join(out)


def resolve_league(query, leagues):
    """Match 'pl', 'laliga', 'eng.1' or 'Premier' to a configured league slug."""
    q = normalise(query)
    if not q:
        return None
    for slug, meta in leagues.items():
        if normalise(slug) == q:
            return slug
    for slug, meta in leagues.items():
        if q in (normalise(meta["short"]), normalise(meta["name"])):
            return slug
    hits = [
        slug for slug, meta in leagues.items()
        if q in normalise(meta["name"]) or q in normalise(meta["short"])
        or q in normalise(slug)
    ]
    if len(hits) > 1:
        warn(f"{query!r} is ambiguous - matched "
             f"{', '.join(leagues[h]['name'] for h in hits)}")
        return None
    return hits[0] if hits else None


def render_table(slug, meta, table, fav_ids, colour):
    """A league table, compactly."""
    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    if not table:
        return f"  No table available for {meta['name']}.\n"

    rows = sorted(table.items(), key=lambda kv: kv[1]["rank"])
    width = max(len(r["name"]) for _, r in rows)

    out = [f"\n  {C(BOLD, meta['name'])}\n"]
    out.append(
        C(DIM, f"  {'#':>3}  {'team':<{width}}  {'P':>2} {'W':>2} {'D':>2} {'L':>2}"
                f" {'GD':>4} {'Pts':>4}")
    )
    for tid, r in rows:
        gd = int(r["gd"])
        line = (
            f"  {r['rank']:>3}  {r['name']:<{width}}  {int(r['played']):>2}"
            f" {int(r['won']):>2} {int(r['drawn']):>2} {int(r['lost']):>2}"
            f" {gd:>+4} {int(r['points']):>4}"
        )
        if tid in fav_ids:
            line = C(RED, line) + C(RED, "  <3")
        out.append(line)
    return "\n".join(out) + "\n"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def notify(matches, tz, top: int) -> None:
    if not shutil.which("notify-send"):
        warn("notify-send not found")
        return
    if not matches:
        subprocess.run(
            ["notify-send", "-a", "footy", "Football", "Nothing on today."], check=False
        )
        return

    lines = []
    for m in matches[:top]:
        kick = datetime.fromisoformat(
            m["event"]["date"].replace("Z", "+00:00")
        ).astimezone(tz)
        mark = " <3" if m["favourite"] else ""
        lines.append(
            f"{kick:%H:%M}  {m['home']['team']['shortDisplayName']} v "
            f"{m['away']['team']['shortDisplayName']}"
            f"  <i>{m['league']['short']}</i>{mark}"
        )
    subprocess.run(
        [
            "notify-send", "-a", "footy", "-i", "applications-games",
            f"{len(matches)} matches today",
            "\n".join(lines),
        ],
        check=False,
    )


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def gather(cfg, day: date, only=None):
    """Score a day's matches. `only` limits which competitions' fixtures are
    fetched; tables are still read for all of them, since they are cached for
    hours and are what gives European opponents a league position."""
    leagues = cfg["leagues"]
    wanted = [s for s in leagues if only is None or s in only]

    with ThreadPoolExecutor(max_workers=min(12, len(leagues) * 2)) as pool:
        fx = {slug: pool.submit(get_fixtures, slug, day) for slug in wanted}
        st = {slug: pool.submit(get_standings, slug, day) for slug in leagues}

        events_by_league, tables = {}, {}
        for slug in wanted:
            try:
                events_by_league[slug] = fx[slug].result()
            except RuntimeError as exc:
                warn(f"{slug}: {exc}")
                events_by_league[slug] = []
        for slug in leagues:
            try:
                tables[slug] = st[slug].result()
            except Exception:
                tables[slug] = {}

    all_events = [e for evs in events_by_league.values() for e in evs]
    fav_ids = resolve_favourites(cfg["favourites"], all_events)

    # One combined domestic table, used to size up clubs in European ties.
    domestic: dict[str, dict] = {}
    for slug, table in tables.items():
        if not is_cross_border(slug):
            domestic.update(table)

    matches = []
    for slug, events in events_by_league.items():
        cross = is_cross_border(slug)
        for event in events:
            scored = score_match(
                event, tables.get(slug, {}), leagues[slug], fav_ids, domestic, cross
            )
            if scored:
                scored["slug"] = slug
                matches.append(scored)

    matches.sort(key=lambda m: (-m["score"], m["event"]["date"]))
    return matches


LOOKAHEAD_DAYS = 21
SCAN_CHUNK = 7      # days fetched per pass
ENOUGH_FIXTURES = 6  # stop scanning once we have this many


def show_live(cfg, day, tz, colour, limit) -> int:
    """Only what is actually being played right now."""
    today = gather(cfg, day)
    matches = [m for m in today if match_state(m) == "in"]
    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)

    if not matches:
        upcoming = sorted(
            (m for m in today if match_state(m) == "pre"),
            key=lambda m: m["event"]["date"],
        )
        if upcoming:
            nxt = upcoming[0]
            print(
                f"\n  Nothing in play. Next up {C(BOLD, kickoff(nxt, tz).strftime('%H:%M'))}"
                f"  {nxt['home']['team']['shortDisplayName']} v "
                f"{nxt['away']['team']['shortDisplayName']}"
                f"  {C(CYAN, nxt['league']['short'])}\n"
            )
        else:
            print("\n  Nothing in play, and nothing left today.\n")
        return 0

    matches.sort(key=lambda m: -m["score"])
    if limit:
        matches = matches[:limit]
    print(render(matches, tz, colour, day))
    return 0


def _safe_fixtures(slug, day):
    try:
        return get_fixtures(slug, day)
    except RuntimeError:
        return []


def show_fixtures(cfg, start, tz, colour, limit) -> int:
    """The next matches for the teams you follow."""
    if not cfg["favourites"]:
        warn("no teams configured; add some to `favourites` in config.toml")
        return 2

    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)
    target = limit or ENOUGH_FIXTURES

    # Liverpool play in two of ten tracked competitions; scanning the other
    # eight for three weeks is most of the work and none of the answer.
    team_ids = resolve_team_ids(cfg, cfg["favourites"])
    relevant = leagues_with(cfg, team_ids) if team_ids else list(cfg["leagues"])

    found = []
    for chunk_start in range(0, LOOKAHEAD_DAYS, SCAN_CHUNK):
        days = [
            start + timedelta(days=i)
            for i in range(chunk_start, min(chunk_start + SCAN_CHUNK, LOOKAHEAD_DAYS))
        ]
        # Warm the cache for every day/league at once - one wide pool beats
        # a pool per day, which serialised on the slowest league each time.
        jobs = [(d, slug) for d in days for slug in relevant]
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda j: _safe_fixtures(j[1], j[0]), jobs))

        for day in days:
            found += [
                (day, m) for m in gather(cfg, day, only=relevant) if m["favourite"]
            ]
        if len(found) >= target:
            break
    if not found:
        who = ", ".join(cfg["favourites"])
        print(f"\n  No {who} fixtures in the next {LOOKAHEAD_DAYS} days.\n")
        return 0
    if limit:
        found = found[:limit]

    out = [f"\n  {C(BOLD, ', '.join(cfg['favourites']))}\n"]
    for day, m in found:
        state = match_state(m)
        when = (
            kickoff(m, tz).strftime("%H:%M") if state == "pre"
            else (C(GREEN, "LIVE ") if state == "in" else C(DIM, "FT   "))
        )
        n = stars(m["score"])
        bar = C(YELLOW, "*" * n) + C(DIM, "." * (5 - n))
        hc, ac = edge_colour(m["edge"], "home"), edge_colour(m["edge"], "away")
        hn = side_text(m["home"], m["home_row"], "H")
        an = side_text(m["away"], m["away_row"], "A")
        hn, an = (C(hc, hn) if hc else hn), (C(ac, an) if ac else an)
        away_days = (day - start).days
        rel = "today" if away_days == 0 else (
            "tomorrow" if away_days == 1 else day.strftime("%a %-d %b")
        )
        out.append(f"  {C(DIM, f'{rel:<12}')}{bar}  {when}  {hn} v {an}  "
                   f"{C(CYAN, m['league']['short'])}")
    print("\n".join(out) + "\n")
    return 0


def colour_wanted(args) -> bool:
    if args.no_color:
        init_colours(force=False)
        return False
    return init_colours(force=args.color)


def show_tables(cfg, query, day, colour) -> int:
    leagues = cfg["leagues"]
    if query:
        slug = resolve_league(query, leagues)
        if not slug:
            warn(f"no league matching {query!r}. Known: {', '.join(leagues)}")
            return 2
        wanted = [slug]
    else:
        wanted = list(leagues)

    with ThreadPoolExecutor(max_workers=min(10, len(wanted))) as pool:
        tables = dict(
            zip(wanted, pool.map(lambda s: get_standings(s, day), wanted))
        )

    # favourites are configured by name, so match them against the tables
    fav_names = [normalise(n) for n in cfg["favourites"]]
    fav_ids = {
        tid
        for table in tables.values()
        for tid, row in table.items()
        if any(f in normalise(row["name"]) or normalise(row["name"]) in f
               for f in fav_names)
    }

    out = [
        render_table(slug, leagues[slug], tables[slug], fav_ids, colour)
        for slug in wanted
    ]
    print("".join(out))
    return 0


def _iptv_cfg(cfg):
    """Load [iptv] - strictly opt-in; never reads anything local on its own."""
    import iptv
    ready = iptv.load(cfg)
    if not ready:
        warn("IPTV not configured; add an [iptv] section to config.toml "
             "(see config.example.toml), or run `footy --import-iptvnator`")
    return ready


def show_import_iptvnator(cfg) -> int:
    """One-shot local helper: pull [iptv] credentials out of iptvnator."""
    import iptv
    if iptv.load(cfg):
        warn("[iptv] already configured; nothing to do")
        return 0
    updated = iptv.import_from_iptvnator(cfg)
    if updated and iptv.load(updated):
        print(f"[iptv] written to {CONFIG_PATH}")
        return 0
    return 2


def _match_filter(matches, limit, min_stars, by_league=False):
    """Apply the display limit/star filter the ranked list uses."""
    if min_stars > 1:
        matches = [m for m in matches if stars(m["score"]) >= min_stars]
    if limit and not by_league:
        matches = matches[:limit]
    return matches


def show_watch(cfg, day, tz, colour, index, limit=0, min_stars=0) -> int:
    """Open a match in your IPTV player: `--watch N` or interactive."""
    import iptv
    iptv_cfg = _iptv_cfg(cfg)
    if not iptv_cfg:
        return 2

    matches = _match_filter(gather(cfg, day), limit, min_stars)
    if not matches:
        print("No matches in your leagues today.")
        return 0

    C = (lambda code, s: f"{code}{s}{RESET}") if colour else (lambda code, s: s)

    if index is None or index == -1:
        index = None
        # Annotate each line with its likely channel, so picking is informed.
        groups = programmes = None
        try:
            groups = iptv.group_streams(iptv.streams(iptv_cfg))
            _, programmes = iptv.epg(iptv_cfg)
        except (RuntimeError, iptv.ET.ParseError) as exc:
            warn(f"iptv: {exc}")
        hints = {}
        if groups is not None and programmes is not None:
            for i, m in enumerate(matches):
                r = iptv.best(m, iptv_cfg, groups=groups, programmes=programmes)
                if r:
                    hints[i] = r

        width = len(str(len(matches)))
        for i, m in enumerate(matches, 1):
            kick = datetime.fromisoformat(
                m["event"]["date"].replace("Z", "+00:00")
            ).astimezone(tz)
            hn = m["home"]["team"]["shortDisplayName"]
            an = m["away"]["team"]["shortDisplayName"]
            state = m["event"]["competitions"][0]["status"]["type"].get("state")
            when = kick.strftime("%H:%M") if state == "pre" else (
                C(GREEN, "LIVE") if state == "in" else C(DIM, "FT  ")
            )
            n = stars(m["score"])
            bar = C(YELLOW, "*" * n) + C(DIM, "." * (5 - n))
            tag = C(CYAN, m["league"]["short"])
            line = f"  {i:>{width}}) {bar}  {when}  {hn} v {an}  {tag}"
            if i - 1 in hints:
                line += C(DIM, f"  [{hints[i - 1]['channel']} "
                               f"{hints[i - 1]['quality']}]")
            print(line)

        try:
            raw = input("open match # (Enter to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            return 130
        if not raw:
            return 0
        try:
            index = int(raw)
        except ValueError:
            warn(f"bad number {raw!r}")
            return 2

    if not 1 <= index <= len(matches):
        warn(f"no match #{index} (1..{len(matches)} available)")
        return 2

    return iptv.watch(matches[index - 1], iptv_cfg)


def show_playlist(cfg, day) -> int:
    """An M3U of today's matches as channels, for importing into a player."""
    import iptv
    iptv_cfg = _iptv_cfg(cfg)
    if not iptv_cfg:
        return 2
    try:
        matches = gather(cfg, day)
        print(iptv.playlist(matches, iptv_cfg))
    except RuntimeError as exc:
        warn(f"iptv: {exc}")
        return 1
    warn("this playlist embeds your provider credentials; keep the file private")
    return 0


def show_iptv_channels(cfg) -> int:
    import iptv
    iptv_cfg = _iptv_cfg(cfg)
    if not iptv_cfg:
        return 2
    try:
        print(iptv.dump_channels(iptv_cfg))
    except RuntimeError as exc:
        warn(f"iptv: {exc}")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="footy", description="What football is worth watching today."
    )
    p.add_argument("-d", "--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("-t", "--tomorrow", action="store_true")
    p.add_argument("-w", "--week", action="store_true", help="next 7 days")
    p.add_argument("-n", "--limit", type=int, help="show only the top N")
    p.add_argument(
        "-s", "--min-stars", type=int, choices=range(1, 6),
        help="hide matches rated below this",
    )
    p.add_argument(
        "-f", "--fixtures", action="store_true",
        help="next matches for the teams you follow",
    )
    p.add_argument(
        "--live", action="store_true", help="only matches being played right now",
    )
    p.add_argument(
        "-T", "--table", nargs="?", const="", metavar="LEAGUE",
        help="show a league table (e.g. -T pl, -T laliga); omit for all",
    )
    p.add_argument(
        "-g", "--by-league", action="store_true",
        help="best matches per competition, instead of one global ranking",
    )
    p.add_argument("--notify", action="store_true", help="send a desktop notification")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-color", action="store_true")
    p.add_argument(
        "--color", action="store_true",
        help="force colour even when piped (e.g. into less -R)",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    p.add_argument(
        "--watch", nargs="?", const=-1, type=int, metavar="N",
        help="open match #N (or pick interactively) in your IPTV player",
    )
    p.add_argument(
        "--playlist", action="store_true",
        help="print an M3U of today's matches as channels, for your IPTV player",
    )
    p.add_argument(
        "--iptv-channels", action="store_true",
        help="list the IPTV provider's channels, grouped by name and quality",
    )
    p.add_argument(
        "--import-iptvnator", action="store_true",
        help="local helper: seed [iptv] credentials from an iptvnator install",
    )
    args = p.parse_args()

    cfg = load_config()
    tz = datetime.now().astimezone().tzinfo or timezone.utc

    if args.date:
        try:
            start = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            warn(f"bad date {args.date!r}, expected YYYY-MM-DD")
            return 2
    elif args.tomorrow:
        start = datetime.now(tz).date() + timedelta(days=1)
    else:
        start = datetime.now(tz).date()

    limit = args.limit if args.limit is not None else int(
        cfg["display"].get("limit", 0)
    )
    min_stars = (
        args.min_stars
        if args.min_stars is not None
        else int(cfg["display"].get("min_stars", 0))
    )

    if args.import_iptvnator:
        return show_import_iptvnator(cfg)

    if args.iptv_channels:
        return show_iptv_channels(cfg)

    if args.playlist:
        return show_playlist(cfg, start)

    if args.watch is not None:
        return show_watch(cfg, start, tz, colour_wanted(args), args.watch,
                          limit, min_stars)

    if args.table is not None:
        return show_tables(cfg, args.table, start, colour_wanted(args))

    if args.live:
        return show_live(cfg, start, tz, colour_wanted(args), args.limit or 0)

    if args.fixtures:
        return show_fixtures(cfg, start, tz, colour_wanted(args), args.limit or 0)

    days = [start + timedelta(days=i) for i in range(7 if args.week else 1)]

    colour = colour_wanted(args)

    chunks = []
    for day in days:
        try:
            matches = gather(cfg, day)
        except Exception as exc:
            warn(f"could not fetch {day}: {exc}")
            return 1

        matches = _match_filter(matches, limit, min_stars, by_league=args.by_league)

        if args.json:
            chunks.append({
                "date": day.isoformat(),
                "matches": [
                    {
                        "kickoff": m["event"]["date"],
                        "home": m["home"]["team"]["displayName"],
                        "away": m["away"]["team"]["displayName"],
                        "league": m["league"]["name"],
                        "score": round(m["score"], 4),
                        "derby": m["derby"],
                        "edge": m["edge"],
                        "favourite": m["favourite"],
                        "status": m["event"]["competitions"][0]["status"]["type"]["state"],
                    }
                    for m in matches
                ],
            })
        elif args.notify:
            notify(matches, tz, int(cfg["display"].get("notify_top", 3)))
        elif args.by_league:
            chunks.append(
                render_grouped(matches, tz, colour, day, max(1, args.limit or 2))
            )
        else:
            chunks.append(render(matches, tz, colour, day))

    if args.json:
        print(json.dumps(chunks if args.week else chunks[0], indent=2))
    elif not args.notify:
        print("".join(chunks))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
