import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import footy
import iptv


# --------------------------------------------------------------------------
# sample data
# --------------------------------------------------------------------------

STREAMS = [
    {"name": "beIN_SPORTS_1_FHD", "stream_id": "11"},
    {"name": "beIN_SPORTS_1_4K", "stream_id": "12"},
    {"name": "beIN_SPORTS_1_SD", "stream_id": "13"},
    {"name": "beIN_SPORTS_2_FHD", "stream_id": "21"},
    {"name": "beIN_SPORTS_2_HD", "stream_id": "22"},
    {"name": "beIN_SPORTS_3_4K", "stream_id": "31"},
    {"name": "beIN_SPORTS_3_FHD", "stream_id": "32"},
    {"name": "beIN_SPORTS_4_HD", "stream_id": "41"},
    {"name": "BeIN Alkass 1 4K", "stream_id": "51"},
    {"name": "BeIN Alkass 1 HD", "stream_id": "52"},
    {"name": "News Channel", "stream_id": "99"},
]

EPG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<tv date="20260904">
  <channel id="beINSports2.qa@MENA"><display-name>beIN SPORTS 2</display-name></channel>
  <channel id="beINSports3.qa@MENA"><display-name>beIN SPORTS 3</display-name></channel>
  <programme start="20260904185000" stop="20260904205000" channel="beINSports2.qa@MENA">
    <title>Ipswich Town vs Liverpool - English Premier League 2026/2027 - Week 3</title>
  </programme>
  <programme start="20260904170000" stop="20260904190000" channel="beINSports3.qa@MENA">
    <title>Real Betis vs Real Madrid - Spanish LaLiga 2026/2027 - Week 4</title>
  </programme>
</tv>"""


def mk_match(home="Ipswich Town", away="Liverpool",
             date="2026-09-04T19:00:00Z", slug="eng.1"):
    return {
        "home": {"team": {"id": "1", "displayName": home, "shortDisplayName": home,
                          "name": home, "abbreviation": home[:3]}},
        "away": {"team": {"id": "2", "displayName": away, "shortDisplayName": away,
                          "name": away, "abbreviation": away[:3]}},
        "event": {"date": date},
        "slug": slug,
        "league": {"name": "Premier League", "short": "PL", "weight": 1.0},
    }


def mk_iptv(**over):
    base = {
        "host": "http://provider.example:2095",
        "username": "user", "password": "pass",
        "player": "", "epg_url": "https://epg.example/guide.xml",
        "quality": ["4K", "FHD", "HD", "SD", "LOW"],
        "channels": {},
    }
    base.update(over)
    return base


def _stub_fetch(monkeypatch, streams=STREAMS, epg_xml=EPG_XML):
    def fake_fetch(url, cache_key=None, ttl=3600, text=False):
        if cache_key == "xt_streams":
            return list(streams)
        if cache_key == "xt_epg":
            return epg_xml if text else epg_xml
        raise AssertionError(f"unexpected fetch: {cache_key or url}")
    monkeypatch.setattr(footy, "fetch", fake_fetch)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_load_requires_credentials():
    assert iptv.load({}) is None
    assert iptv.load({"iptv": {"host": "h"}}) is None  # missing user/pass
    cfg = iptv.load({"iptv": {"host": "h", "username": "u", "password": "p"}})
    assert cfg["host"] == "h"
    assert cfg["epg_url"] == iptv.DEFAULT_EPG_URL  # default filled
    assert cfg["player"] == ""


def test_import_from_iptvnator_seeds(tmp_path, monkeypatch):
    db = tmp_path / "database.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE playlists (serverUrl TEXT, username TEXT, "
                "password TEXT, type TEXT)")
    con.execute("INSERT INTO playlists VALUES (?, ?, ?, 'xtream')",
                ("http://p.example:8080", "alice", "s3cret"))
    con.commit()
    con.close()

    conf = tmp_path / "config.toml"
    conf.write_text("favourites = [\"Liverpool\"]\n", encoding="utf-8")
    monkeypatch.setattr(iptv, "IPTVNATOR_DB", db)
    monkeypatch.setattr(footy, "CONFIG_PATH", conf)

    cfg = iptv.import_from_iptvnator({"favourites": []})
    assert cfg["iptv"]["host"] == "http://p.example:8080"
    assert cfg["iptv"]["username"] == "alice"
    assert cfg["iptv"]["password"] == "s3cret"
    assert "s3cret" in conf.read_text(encoding="utf-8")
    # credentials file must not be world-readable
    assert conf.stat().st_mode & 0o077 == 0


def test_import_from_iptvnator_noop_when_configured(tmp_path, monkeypatch):
    conf = tmp_path / "config.toml"
    conf.write_text("", encoding="utf-8")
    monkeypatch.setattr(footy, "CONFIG_PATH", conf)
    monkeypatch.setattr(iptv, "IPTVNATOR_DB", tmp_path / "missing.db")
    cfg = {"iptv": {"host": "h", "username": "u", "password": "p"}}
    assert iptv.import_from_iptvnator(cfg) is None  # never overwrites
    assert conf.read_text(encoding="utf-8") == ""


def test_import_from_iptvnator_no_db(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text("", encoding="utf-8")
    monkeypatch.setattr(footy, "CONFIG_PATH", conf)
    monkeypatch.setattr(iptv, "IPTVNATOR_DB", tmp_path / "missing.db")
    assert iptv.import_from_iptvnator({"favourites": []}) is None
    assert "no iptvnator database" in capsys.readouterr().err


# --------------------------------------------------------------------------
# name handling
# --------------------------------------------------------------------------

def test_split_tokens_drop_quality_but_keep_numbers():
    assert iptv._split_tokens("beIN_SPORTS_2_FHD") == {"bein", "sports", "2"}
    assert iptv._split_tokens("beIN_SPORTS1_4K") == {"bein", "sports", "1"}
    assert iptv._split_tokens("beIN_SPORTS_1_English_1080p") == {
        "bein", "sports", "1", "english"}
    assert iptv._split_tokens("[FR]_BeIN_SPORTS_1_HD") == {
        "french", "bein", "sports", "1"}
    assert iptv._split_tokens("BeIN Alkass 1 4K") == {"bein", "alkass", "1"}


def test_quality_rank():
    assert iptv._quality_rank("beIN_SPORTS_1_4K") == 0
    assert iptv._quality_rank("beIN_SPORTS_1_FHD") == 1
    assert iptv._quality_rank("beIN_SPORTS_1_HD") == 3
    assert iptv._quality_rank("beIN_SPORTS_1_SD") == 4
    assert iptv._quality_rank("beIN_SPORTS_1_Low") == 5


def test_group_streams_merges_variants():
    groups = iptv.group_streams(STREAMS)
    by_key = {frozenset(g["key"]): g for g in groups}
    one = by_key[frozenset({"bein", "sports", "1"})]
    assert len(one["streams"]) == 3
    assert one["best"]["name"] == "beIN_SPORTS_1_4K"
    # the two beIN 2 variants group together, distinct from beIN 3
    assert frozenset({"bein", "sports", "2"}) in by_key
    assert frozenset({"bein", "sports", "3"}) in by_key


# --------------------------------------------------------------------------
# EPG
# --------------------------------------------------------------------------

def test_parse_epg_time():
    dt = iptv._parse_epg_time("20260904185000")
    assert dt.isoformat() == "2026-09-04T18:50:00+00:00"
    dt = iptv._parse_epg_time("20260904185000 +0300")
    assert dt.isoformat() == "2026-09-04T15:50:00+00:00"


def test_epg_parses_channels_and_programmes(monkeypatch):
    _stub_fetch(monkeypatch)
    names, programmes = iptv.epg(mk_iptv())
    assert names["beINSports2.qa@MENA"] == "beIN SPORTS 2"
    assert len(programmes) == 2
    assert programmes[0]["title"].startswith("Ipswich Town")


def test_match_programme_both_orders():
    p = {"title": "Ipswich Town vs Liverpool - English Premier League"}
    assert iptv.match_programme(mk_match(), p)
    p2 = {"title": "Liverpool v Ipswich Town - League"}
    assert iptv.match_programme(mk_match(), p2)
    p3 = {"title": "Ipswich Town vs Norwich - League"}
    assert not iptv.match_programme(mk_match(), p3)


def test_epg_hits_uses_kickoff_window():
    kickoff = iptv.kickoff(mk_match())  # 19:00Z
    near = {"start": kickoff - timedelta(minutes=10),
            "title": "Ipswich Town vs Liverpool - EPL", "channel": "c", "name": "c"}
    far = {"start": kickoff + timedelta(hours=3),
           "title": "Ipswich Town vs Liverpool - EPL", "channel": "c", "name": "c"}
    hits = iptv.epg_hits(mk_match(), [near, far])
    assert hits == [near]


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def test_resolve_channel_maps_epg_name_to_group():
    groups = iptv.group_streams(STREAMS)
    g2 = iptv.resolve_channel("beIN SPORTS 2", groups)
    assert g2["key"] == frozenset({"bein", "sports", "2"})
    g3 = iptv.resolve_channel("beIN SPORTS 3", groups)
    assert g3["key"] == frozenset({"bein", "sports", "3"})
    assert g2 is not g3
    assert iptv.resolve_channel("Nonsense Channel", groups) is None


def test_pick_quality_preference():
    groups = iptv.group_streams(STREAMS)
    one = next(g for g in groups if g["key"] == frozenset({"bein", "sports", "1"}))
    assert iptv.pick_quality(one, mk_iptv())["name"] == "beIN_SPORTS_1_4K"
    # preference that omits 4K falls to FHD
    assert iptv.pick_quality(one, mk_iptv(quality=["FHD", "HD"]))["name"] == "beIN_SPORTS_1_FHD"


def test_best_resolves_match_to_channel(monkeypatch):
    _stub_fetch(monkeypatch)
    resolved = iptv.best(mk_match(), mk_iptv())
    assert resolved["channel"] == "beIN SPORTS 2"
    assert resolved["quality"] == "FHD"
    assert resolved["url"].endswith("/21")  # beIN_SPORTS_2_FHD stream
    assert "user:pass" not in resolved["url"]


def test_best_returns_none_for_uncovered_match(monkeypatch):
    _stub_fetch(monkeypatch)
    assert iptv.best(mk_match("Genoa", "Como"), mk_iptv()) is None


def test_stream_url():
    assert iptv._stream_url(mk_iptv(), {"stream_id": "77"}) == \
        "http://provider.example:2095/user/pass/77"


# --------------------------------------------------------------------------
# watch / playlist / picker
# --------------------------------------------------------------------------

def test_watch_launches_player(monkeypatch):
    _stub_fetch(monkeypatch)
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(argv)

    monkeypatch.setattr(iptv.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(iptv, "_player", lambda i: "mpv")
    rc = iptv.watch(mk_match(), mk_iptv())
    assert rc == 0
    assert calls == [["mpv", "http://provider.example:2095/user/pass/21"]]


def test_watch_player_with_args(monkeypatch):
    _stub_fetch(monkeypatch)
    calls = []
    monkeypatch.setattr(iptv.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(iptv, "_player", lambda i: "mpv --fs --no-border")
    rc = iptv.watch(mk_match(), mk_iptv())
    assert rc == 0
    assert calls == [["mpv", "--fs", "--no-border",
                      "http://provider.example:2095/user/pass/21"]]


def test_watch_picker_fallback(monkeypatch, tmp_path):
    # an EPG-less match (no programmes) forces the picker
    _stub_fetch(monkeypatch, streams=STREAMS, epg_xml="<tv></tv>")
    calls = []
    monkeypatch.setattr(iptv.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(iptv, "_player", lambda i: "mpv")
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    monkeypatch.setattr(iptv, "STATE_FILE", tmp_path / "watchstate.json")
    rc = iptv.watch(mk_match("Genoa", "Como", slug="ita.1"), mk_iptv())
    assert rc == 0
    assert calls, "expected a stream to be launched from the picker"


def test_playlist_contains_matches(monkeypatch):
    _stub_fetch(monkeypatch)
    pl = iptv.playlist([mk_match(), mk_match("Real Betis", "Real Madrid")], mk_iptv())
    assert "#EXTM3U" in pl
    assert "Ipswich Town v Liverpool" in pl
    assert "http://provider.example:2095/user/pass/21" in pl


def test_dump_channels_lists_groups(monkeypatch):
    _stub_fetch(monkeypatch)
    out = iptv.dump_channels(mk_iptv())
    assert "channels" in out
    assert "Bein Sports 1" in out
    assert "Bein Alkass 1" in out