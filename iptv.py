"""iptv - resolve a footy match to a watchable stream.

Talks to an Xtream Codes provider (the same one iptvnator is configured with)
plus a public beIN MENA XMLTV EPG, so a fixture's channel is usually found
automatically. The interactive picker is the fallback for anything the EPG
cannot name.

Streams are fetched via the provider's player_api; the EPG URL is
configurable and defaults to a community-mirrored feed scraped from
beinsports.com. Both are disk-cached like the rest of footy.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import footy

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_EPG_URL = "https://al7omed.github.io/bein-epg/guide.xml"
DEFAULT_QUALITY = ["4K", "FHD", "HD", "SD", "LOW"]
IPTVNATOR_DB = Path.home() / ".config" / "com.4gray.dev" / "database.db"

STATE_FILE = footy.CACHE_DIR / "watchstate.json"

# Quality tokens that identify a stream's bitrate; smaller rank = better.
QUALITY_TOKENS = {
    "4k": 0, "uhd": 0, "ultra": 0,
    "fhd": 1, "1080p": 1, "1080fhd": 1, "1080": 1,
    "vega": 2, "h265": 2, "hevc": 2, "265": 2,
    "hd": 3, "hdd": 3, "hd720": 3, "720": 3, "hq": 3,
    "sd": 4, "512": 5, "512k": 5, "low": 5,
}
QUALITY_LABEL = {0: "4K", 1: "FHD", 2: "HD", 3: "HD", 4: "SD", 5: "LOW"}

# Feed-language / regional spellings that should compare equal.
SYNONYMS = {"sport": "sports", "en": "english", "fr": "french"}
NOISE = {"p", "k", "h", "m", "t", "hdq"}

# Names that make a channel a live-sports candidate for the picker.
SPORTS_FAMILY = {"bein", "alkass", "sport", "max", "xtra", "premier",
                 "eurosport", "arena", "eleven", "events"}


def load(cfg: dict) -> dict | None:
    """The [iptv] block with defaults filled in, or None when unconfigured."""
    iptv = cfg.get("iptv")
    if not isinstance(iptv, dict):
        return None
    if not (iptv.get("host") and iptv.get("username") and iptv.get("password")):
        return None
    return {
        "host": str(iptv["host"]).rstrip("/"),
        "username": str(iptv["username"]),
        "password": str(iptv["password"]),
        "player": iptv.get("player") or "",
        "epg_url": iptv.get("epg_url") or DEFAULT_EPG_URL,
        "quality": list(iptv.get("quality") or DEFAULT_QUALITY),
        "channels": iptv.get("channels") or {},
    }


def import_from_iptvnator(cfg: dict) -> dict | None:
    """One-shot: seed [iptv] credentials from an iptvnator installation.

    This is a local convenience only - it reads iptvnator's Electron SQLite
    database on this machine and writes the Xtream credentials into
    config.toml (which is gitignored). It is never run automatically; call it
    explicitly with `footy --import-iptvnator`. Returns a freshly loaded
    config with [iptv], or None when [iptv] is already set, iptvnator is not
    installed, or nothing could be recovered.
    """
    if load(cfg):
        footy.warn("[iptv] already configured; leaving it alone")
        return None
    if not IPTVNATOR_DB.exists():
        footy.warn("no iptvnator database found; add an [iptv] section to "
                   f"{footy.CONFIG_PATH} (see config.example.toml)")
        return None
    try:
        con = sqlite3.connect(f"file:{IPTVNATOR_DB}?mode=ro", uri=True)
        row = con.execute(
            "SELECT serverUrl, username, password FROM playlists "
            "WHERE type='xtream' AND username IS NOT NULL AND username!='' "
            "LIMIT 1"
        ).fetchone()
        con.close()
    except Exception as exc:
        footy.warn(f"could not read iptvnator database: {exc}")
        return None
    if not row or not all(row):
        footy.warn("no xtream account found in the iptvnator database")
        return None

    host, username, password = row
    try:
        with open(footy.CONFIG_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                "\n# IPTV - credentials are local to this machine (gitignored).\n"
                "[iptv]\n"
                f"host = \"{host}\"\n"
                f"username = \"{username}\"\n"
                f"password = \"{password}\"\n"
                "# player = \"mpv\"      # or vlc; auto-detected if unset\n"
                "# epg_url = \"https://al7omed.github.io/bein-epg/guide.xml\"\n"
            )
    except OSError as exc:
        footy.warn(f"could not write {footy.CONFIG_PATH}: {exc}")
        return None
    try:
        footy.CONFIG_PATH.chmod(0o600)  # credentials live here; keep it private
    except OSError:
        pass
    footy.warn("seeded [iptv] credentials from iptvnator; edit "
               f"{footy.CONFIG_PATH} to adjust")
    return footy.load_config()


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def streams(iptv: dict) -> list[dict]:
    """Every live channel from the provider's player_api (cached ~6h)."""
    q = urllib.parse.urlencode({
        "username": iptv["username"], "password": iptv["password"],
        "action": "get_live_streams",
    })
    url = f"{iptv['host']}/player_api.php?{q}"
    return footy.fetch(url, cache_key="xt_streams", ttl=6 * 3600)


# Multi-part quality words, matched whole (longest first).
_QUALITY_RE = re.compile(
    r"\b(1080fhd|1080p|1080|uhd|4k|512k|512|hd720|720|h265|265|hevc|hdd|"
    r"fhd|vega|ultra|hq|hd|sd|low)\b"
)


def _normalise_tokens(name: str) -> str:
    n = footy.normalise(name).replace("be in", "bein")
    n = re.sub(r"beinsports|beinsport|beinalkass", "bein sports", n)
    return n


def _split_tokens(name: str) -> set[str]:
    """Word tokens of a channel name: quality/region-noise removed."""
    n = _QUALITY_RE.sub(" ", _normalise_tokens(name))
    toks = set()
    for part in re.findall(r"[a-z]+|[0-9]+", n):
        if part in NOISE:
            continue
        part = SYNONYMS.get(part, part)
        if part.isdigit() or len(part) > 1:
            toks.add(part)
    return toks


def _quality_rank(name: str) -> int:
    n = _normalise_tokens(name)
    ranks = [QUALITY_TOKENS[m.group(1)] for m in _QUALITY_RE.finditer(n)]
    return min(ranks) if ranks else 5


def _pretty(tokens: set[str]) -> str:
    """Channel name for humans: 'Bein Alkass 1', numbers last."""
    words = [t for t in tokens if not t.isdigit()]
    nums = sorted(t for t in tokens if t.isdigit())
    if "bein" in words:
        words = ["bein"] + sorted(w for w in words if w != "bein")
    return " ".join(words + nums).title()


def group_streams(streams: list[dict]) -> list[dict]:
    """Channels with their quality variants merged under one token set."""
    groups: dict[frozenset, dict] = {}
    for s in streams:
        name = s.get("name", "")
        key = frozenset(_split_tokens(name))
        if not key:
            continue
        g = groups.setdefault(key, {"key": key, "streams": []})
        g["streams"].append(s)
    for g in groups.values():
        g["streams"].sort(key=lambda s: _quality_rank(s.get("name", "")))
        g["best"] = g["streams"][0]
    return list(groups.values())


def _parse_epg_time(value: str) -> datetime | None:
    """XMLTV 'YYYYMMDDHHMMSS' with optional ' +HHMM' offset, UTC default."""
    m = re.match(r"^(\d{8})(\d{6})(?:\s*([+-]\d{4}))?$", value.strip())
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    dt = dt.replace(tzinfo=timezone.utc)
    if m.group(3):
        sign = 1 if m.group(3)[0] == "+" else -1
        off = int(m.group(3)[1:3]) * 60 + int(m.group(3)[3:])
        dt -= timedelta(minutes=sign * off)
    return dt


def epg(iptv: dict) -> tuple[dict[str, str], list[dict]]:
    """Parse the XMLTV guide into (channel-id -> display name, programmes).

    Each programme is {channel, name, start, stop, title}.
    """
    xml = footy.fetch(iptv["epg_url"], cache_key="xt_epg", ttl=6 * 3600, text=True)
    root = ET.fromstring(xml)

    names = {ch.get("id", ""): (ch.findtext("display-name") or ch.get("id", ""))
             for ch in root.findall("channel")}

    programmes = []
    for p in root.findall("programme"):
        title = p.findtext("title")
        start = _parse_epg_time(p.get("start", ""))
        if not title or start is None:
            continue
        stop = _parse_epg_time(p.get("stop", "")) or start
        programmes.append({
            "channel": p.get("channel", ""),
            "name": names.get(p.get("channel", ""), p.get("channel", "")),
            "start": start,
            "stop": stop,
            "title": title,
        })
    return names, programmes


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def _team_variants(match: dict) -> list[set[str]]:
    """Normalised name variants for each side, for matching EPG titles."""
    out = []
    for side in ("home", "away"):
        team = match[side]["team"]
        fields = {
            footy.normalise(t) for t in (
                team.get(k) for k in footy.TEAM_FIELDS
            ) if t
        }
        fields |= {footy.normalise(footy.expand(f)) for f in fields}
        out.append({f for f in fields if len(f) >= 3})
    return out


def match_programme(match: dict, programme: dict) -> bool:
    """Both sides appear in the programme title (either order)."""
    variants = _team_variants(match)
    title = footy.normalise(programme["title"])
    return all(any(v in title for v in side) for side in variants)


def kickoff(match: dict) -> datetime:
    return datetime.fromisoformat(
        match["event"]["date"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def epg_hits(match: dict, programmes: list[dict], window: int = 60) -> list[dict]:
    """Programmes airing around kickoff that name both sides, best first."""
    k = kickoff(match)
    lo, hi = k - timedelta(minutes=window), k + timedelta(minutes=window)
    hits = [
        p for p in programmes
        if lo <= p["start"] <= hi and match_programme(match, p)
    ]
    hits.sort(key=lambda p: abs((p["start"] - k).total_seconds()))
    return hits


def _channel_score(epg_tokens: set[str], group_tokens: set[str]) -> float:
    inter = epg_tokens & group_tokens
    if not inter:
        return float("-inf")
    missing = epg_tokens - group_tokens
    return len(inter) - 0.5 * len(missing)


def resolve_channel(channel_name: str, groups: list[dict]) -> dict | None:
    """Map an EPG channel name to the provider channel group that fits best."""
    epg_tokens = _split_tokens(channel_name)
    if not epg_tokens:
        return None
    scored = []
    for g in groups:
        score = _channel_score(epg_tokens, g["key"])
        if score == float("-inf"):
            continue
        scored.append((score, -len(g["key"]), g))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    best = scored[-1][2]
    if scored[-1][0] < 2.0:
        return None
    return best


def pick_quality(group: dict, iptv: dict) -> dict:
    """Best stream in a group honouring the configured quality preference."""
    want = {q.upper() for q in iptv.get("quality") or DEFAULT_QUALITY}
    ranked = sorted(group["streams"], key=lambda s: _quality_rank(s["name"]))
    best = ranked[0]
    if want:
        for s in ranked:
            if QUALITY_LABEL[_quality_rank(s["name"])] in want:
                best = s
                break
    return best


def _stream_url(iptv: dict, stream: dict) -> str:
    return f"{iptv['host']}/{iptv['username']}/{iptv['password']}/{stream['stream_id']}"


def _display(group: dict, stream: dict) -> str:
    return f"{_pretty(group['key'])} ({QUALITY_LABEL[_quality_rank(stream['name'])]})"


# --------------------------------------------------------------------------
# picker (fallback)
# --------------------------------------------------------------------------

def _state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        footy.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _hint_for(match: dict, iptv: dict, groups: list[dict]) -> dict | None:
    """A configured [iptv.channels] hint for this match's league."""
    hint = iptv.get("channels", {}).get(match.get("slug"), "")
    if not hint:
        return None
    wanted = frozenset(_split_tokens(hint))
    if not wanted:
        return None
    for g in groups:
        if wanted & g["key"]:
            return g
    return None


def sports_candidates(groups: list[dict]) -> list[dict]:
    return [g for g in groups if g["key"] & SPORTS_FAMILY]


def pick_channel(match: dict, iptv: dict, groups: list[dict],
                 default: str | None = None) -> dict | None:
    """Interactive fuzzy choice of a channel group; returns the chosen group."""
    state = _state()
    pre = state.get(match.get("slug"))
    candidates = sports_candidates(groups)
    hint = _hint_for(match, iptv, groups)
    if hint and hint not in candidates:
        candidates.insert(0, hint)

    # Default: most recent pick, else the configured hint.
    wanted = None
    for name in (pre, default, hint and " ".join(sorted(hint["key"]))):
        if name:
            wanted = frozenset(_split_tokens(name))
            break
    default_idx = 0
    if wanted:
        for i, g in enumerate(candidates):
            if g["key"] == wanted:
                default_idx = i
                break

    footy.warn(f"no EPG match for {match['home']['team']['displayName']} v "
               f"{match['away']['team']['displayName']}; pick a channel:")
    show = candidates[:40]
    for i, g in enumerate(show, 1):
        marker = ">" if i - 1 == default_idx else " "
        best = pick_quality(g, iptv)
        print(f"  {marker}{i:>2}) {_display(g, best)}")
    if len(candidates) > len(show):
        print(f"  ... {len(candidates) - len(show)} more sports channels")

    try:
        raw = input("channel # (Enter for default): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw:
        try:
            default_idx = int(raw) - 1
        except ValueError:
            footy.warn(f"bad number {raw!r}")
            return None
    if not 0 <= default_idx < len(show):
        footy.warn("selection out of range")
        return None

    state[match.get("slug")] = " ".join(sorted(show[default_idx]["key"]))
    _save_state(state)
    return show[default_idx]


# --------------------------------------------------------------------------
# watch / playlist
# --------------------------------------------------------------------------

def _player(iptv: dict) -> str | None:
    if iptv.get("player"):
        return iptv["player"]
    return shutil.which("mpv") or shutil.which("vlc")


def best(match: dict, iptv: dict, groups: list[dict] | None = None,
         programmes: list[dict] | None = None) -> dict | None:
    """Resolve a match to {channel, quality, stream, url}, or None."""
    if groups is None:
        try:
            groups = group_streams(streams(iptv))
        except RuntimeError as exc:
            footy.warn(f"could not reach the IPTV provider: {exc}")
            return None
    if programmes is None:
        try:
            _, programmes = epg(iptv)
        except (RuntimeError, ET.ParseError) as exc:
            footy.warn(f"EPG unavailable: {exc}")
            return None

    for hit in epg_hits(match, programmes):
        group = resolve_channel(hit["name"], groups)
        if not group:
            continue
        stream = pick_quality(group, iptv)
        return {
            "channel": hit["name"],
            "group": group,
            "stream": stream,
            "quality": QUALITY_LABEL[_quality_rank(stream["name"])],
            "url": _stream_url(iptv, stream),
        }
    return None


def watch(match: dict, iptv: dict) -> int:
    """Open the match: EPG-resolved channel, or interactive picker."""
    try:
        groups = group_streams(streams(iptv))
    except RuntimeError as exc:
        footy.warn(f"could not reach the IPTV provider: {exc}")
        return 1
    if not groups:
        footy.warn("no channels fetched from the provider")
        return 1

    resolved = best(match, iptv, groups=groups)
    group = None
    if resolved:
        group = resolved["group"]
        label = f"{resolved['channel']} {resolved['quality']}"
    else:
        state = _state()
        default = state.get(match.get("slug"))
        group = pick_channel(match, iptv, groups, default=default)
        if group is None:
            return 2
        stream = pick_quality(group, iptv)
        label = _display(group, stream)

    stream = pick_quality(group, iptv)
    url = _stream_url(iptv, stream)
    player = _player(iptv)
    who = f"{match['home']['team']['displayName']} v {match['away']['team']['displayName']}"
    if not player:
        footy.warn("no player found; set `player` in [iptv] or install mpv/vlc")
        footy.warn(f"stream: {url}")
        return 3
    if iptv["host"].startswith("http://"):
        footy.warn("provider is http:// - the stream URL (with credentials) "
                   "travels in plaintext")

    print(f"  opening {label}  {who}")
    try:
        subprocess.Popen(
            shlex.split(player) + [url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        footy.warn(f"could not launch {player}: {exc}")
        footy.warn(f"stream: {url}")
        return 3
    return 0


def playlist(matches: list[dict], iptv: dict) -> str:
    """An M3U where each match is a channel named after the fixture."""
    groups = group_streams(streams(iptv))
    _, programmes = epg(iptv)
    lines = ["#EXTM3U", ""]
    for m in matches:
        resolved = best(m, iptv, groups=groups, programmes=programmes)
        if not resolved:
            continue
        url = resolved["url"]
        name = (f"{m['home']['team']['displayName']} v "
                f"{m['away']['team']['displayName']} "
                f"[{resolved['channel']} {resolved['quality']}]")
        lines.append(f"#EXTINF:0 tvg-name=\"{name}\" group-title=\"footy\",{name}")
        lines.append(url)
    lines.append("")
    return "\n".join(lines)


def dump_channels(iptv: dict) -> str:
    """Grouped channel list, so a user can fill [iptv.channels] hints."""
    groups = group_streams(streams(iptv))
    out = [f"  {len(groups)} channels"]
    for g in sorted(groups, key=lambda g: " ".join(sorted(g["key"]))):
        best = pick_quality(g, iptv)
        out.append(
            f"  {_pretty(g['key']):<32} "
            f"{len(g['streams']):>2} variants, best "
            f"{QUALITY_LABEL[_quality_rank(best['name'])]}"
        )
    return "\n".join(out)