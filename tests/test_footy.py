import json

import pytest

import footy


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def event(
    home_id, away_id, home_name="Home", away_name="Away",
    state="pre", home_form="WWWWW", away_form="WWWWW",
    home_score=None, away_score=None, date="2026-09-04T19:00:00Z",
):
    competitors = [
        {
            "homeAway": "home",
            "team": {
                "id": home_id, "displayName": home_name,
                "shortDisplayName": home_name, "name": home_name,
                "abbreviation": home_name[:3],
            },
            "score": home_score, "form": home_form,
        },
        {
            "homeAway": "away",
            "team": {
                "id": away_id, "displayName": away_name,
                "shortDisplayName": away_name, "name": away_name,
                "abbreviation": away_name[:3],
            },
            "score": away_score, "form": away_form,
        },
    ]
    return {
        "date": date,
        "competitions": [{
            "competitors": competitors,
            "status": {"type": {"state": state, "displayClock": "45'"}},
        }],
    }


def table_row(
    team_id, name, rank, total=20, played=20, points=30, gf=30, ga=20,
    won=9, tied=3, lost=8,
):
    return {
        team_id: {
            "rank": rank, "name": name, "points": points, "played": played,
            "gf": gf, "ga": ga, "won": won, "drawn": tied, "lost": lost,
            "gd": gf - ga, "total": total,
        }
    }


def standings_entries(rows):
    entries = []
    for tid, r in rows.items():
        entries.append({
            "team": {"id": tid, "displayName": r["name"],
                     "shortDisplayName": r["name"]},
            "stats": [
                {"name": "rank", "value": r["rank"]},
                {"name": "points", "value": r["points"]},
                {"name": "gamesPlayed", "value": r["played"]},
                {"name": "pointsFor", "value": r["gf"]},
                {"name": "pointsAgainst", "value": r["ga"]},
                {"name": "wins", "value": r["won"]},
                {"name": "ties", "value": r["drawn"]},
                {"name": "losses", "value": r["lost"]},
                {"name": "pointDifferential", "value": r["gd"]},
            ],
        })
    return entries


LEAGUE = {"name": "Premier League", "short": "PL", "weight": 1.0}


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def test_normalise():
    assert footy.normalise("Atlético") == "atletico"
    assert footy.normalise("Man Utd") == "man utd"
    assert footy.normalise("  C.Palace! ") == "c palace"
    assert footy.normalise("FC Bayern München") == "fc bayern münchen".replace("ü", "u")


def test_expand_contractions():
    assert footy.expand("man utd") == "man united"
    assert footy.expand("st pauli") == "saint pauli"
    assert footy.expand("gladbach") == "monchengladbach"


def test_stars_absolute():
    assert footy.stars(0.10) == 1
    assert footy.stars(0.38) == 2
    assert footy.stars(0.48) == 3
    assert footy.stars(0.58) == 4
    assert footy.stars(0.70) == 5
    assert footy.stars(0.99) == 5


def test_goal_appeal():
    rows = [{"played": 20, "gf": 20, "ga": 10}, {"played": 20, "gf": 30, "ga": 20}]
    # (1.5 + 2.5) / 2 = 2.0 goals a game -> floor, 0.0
    assert footy.goal_appeal(rows) == pytest.approx(0.0)
    rows = [{"played": 10, "gf": 30, "ga": 10}]  # 4.0 a game -> 1.0
    assert footy.goal_appeal(rows) == pytest.approx(1.0)
    assert footy.goal_appeal([]) is None


def test_is_cross_border():
    assert footy.is_cross_border("uefa.champions")
    assert footy.is_cross_border("uefa.europa")
    assert footy.is_cross_border("conmebol.1")
    assert not footy.is_cross_border("eng.1")


def test_form_wins():
    assert footy.form_wins("WWWWW") == 5
    assert footy.form_wins("LDLDL") == 0
    assert footy.form_wins(None) == 0


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def test_load_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(footy, "CONFIG_PATH", tmp_path / "nope.toml")
    cfg = footy.load_config()
    assert cfg["favourites"] == []
    # the shipped default matches the example file's 10 leagues
    assert set(cfg["leagues"]) == {
        "eng.1", "eng.2", "esp.1", "ita.1", "ger.1", "ger.2",
        "fra.1", "ned.1", "uefa.champions", "uefa.europa",
    }
    assert cfg["leagues"]["uefa.champions"]["weight"] == 1.15


def test_load_config_merge(tmp_path, monkeypatch):
    conf = tmp_path / "config.toml"
    conf.write_text(
        "favourites = [\"Liverpool\"]\n"
        "[display]\n"
        "min_stars = 4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(footy, "CONFIG_PATH", conf)
    cfg = footy.load_config()
    assert cfg["favourites"] == ["Liverpool"]
    assert cfg["display"]["min_stars"] == 4
    assert cfg["display"]["limit"] == 0  # untouched default preserved
    # leagues still come from defaults when not present in the file
    assert "eng.1" in cfg["leagues"]


# --------------------------------------------------------------------------
# favourite resolution
# --------------------------------------------------------------------------

def test_resolve_favourites_basic():
    events = [
        event("1", "2", "Arsenal", "Chelsea"),
        event("3", "4", "Liverpool", "Everton"),
    ]
    ids = footy.resolve_favourites(["Arsenal"], events)
    assert ids == {"1"}
    ids = footy.resolve_favourites(["liverpool", "everton"], events)
    assert ids == {"3", "4"}


def test_resolve_favourites_contraction():
    events = [event("1", "2", "Borussia Monchengladbach", "Chelsea")]
    ids = footy.resolve_favourites(["gladbach"], events)
    assert ids == {"1"}


def test_resolve_favourites_one_way_matching():
    # "Chelsea" must not match Manchester United's name containing "che"
    events = [event("1", "2", "Manchester United", "Manchester City")]
    assert footy.resolve_favourites(["Chelsea"], events) == set()


def test_resolve_favourites_ambiguous_warns(capsys):
    events = [
        event("1", "2", "Manchester United", "Everton"),
        event("3", "4", "Manchester City", "Chelsea"),
    ]
    ids = footy.resolve_favourites(["manchester"], events)
    assert ids == {"1", "3"}
    assert "ambiguous" in capsys.readouterr().err


# --------------------------------------------------------------------------
# league resolution
# --------------------------------------------------------------------------

def test_resolve_league():
    leagues = footy.DEFAULT_CONFIG["leagues"]
    assert footy.resolve_league("pl", leagues) == "eng.1"
    assert footy.resolve_league("eng.1", leagues) == "eng.1"
    assert footy.resolve_league("laliga", leagues) == "esp.1"
    assert footy.resolve_league("Premier", leagues) == "eng.1"
    assert footy.resolve_league("nonsense", leagues) is None


def test_resolve_league_ambiguous_warns(capsys):
    # "liga" is a substring of both LaLiga and Bundesliga
    leagues = footy.DEFAULT_CONFIG["leagues"]
    result = footy.resolve_league("liga", leagues)
    assert result is None
    assert "ambiguous" in capsys.readouterr().err


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _big_table():
    table = {}
    table.update(table_row("h", "Home Big", 1, played=20, points=45, gf=40, ga=10, won=14))
    table.update(table_row("a", "Away Big", 2, played=20, points=42, gf=38, ga=12, won=13))
    return table


def test_score_match_top_clash_beats_relegation_scrap():
    top_table = _big_table()
    bottom = {}
    bottom.update(table_row("h", "Home Low", 19, played=20, points=15, gf=15, ga=40, won=3))
    bottom.update(table_row("a", "Away Low", 20, played=20, points=12, gf=12, ga=42, won=2))

    top = footy.score_match(event("h", "a"), top_table, LEAGUE, set())
    scrap = footy.score_match(event("h", "a"), bottom, LEAGUE, set())
    assert top["score"] > scrap["score"]


def test_score_match_derby_boost():
    table = _big_table()
    normal = footy.score_match(event("h", "a"), table, LEAGUE, set())
    # 83 v 86 is El Clasico
    derby = footy.score_match(event("83", "86"), table, LEAGUE, set())
    assert derby["score"] > normal["score"]
    assert derby["derby"] == "El Clasico"


def test_score_match_favourite_boost():
    table = _big_table()
    plain = footy.score_match(event("h", "a"), table, LEAGUE, set())
    fav = footy.score_match(event("h", "a"), table, LEAGUE, {"h"})
    assert fav["favourite"] is True
    assert fav["score"] > plain["score"]


def test_score_match_why_top_clash():
    # rank 1 v 2, both played 20 -> confident table, both strong, tight gap
    m = footy.score_match(event("h", "a"), _big_table(), LEAGUE, set())
    assert "top-of-the-table clash" in m["why"]
    assert "six-pointer" in m["why"]


def test_score_match_why_derby():
    m = footy.score_match(event("83", "86"), _big_table(), LEAGUE, set())
    assert m["why"][0] == "El Clasico"


def test_score_match_why_early_season_silent():
    # Two games in: the table hasn't earned position-based claims.
    early = {}
    early.update(table_row("h", "Home Big", 1, played=2, points=6, gf=4, ga=1, won=2))
    early.update(table_row("a", "Away Big", 2, played=2, points=3, gf=2, ga=3, won=1))
    m = footy.score_match(event("h", "a"), early, LEAGUE, set())
    assert "top-of-the-table clash" not in m["why"]
    assert "six-pointer" not in m["why"]
    # form is trustworthy even early, so it still shows
    assert "both in form" in m["why"]


def test_render_shows_why_line():
    from datetime import date
    matches = [footy.score_match(event("h", "a"), _big_table(), LEAGUE, set())]
    out = footy.render(matches, footy.timezone.utc, colour=False,
                       show_date=date(2026, 9, 4))
    assert "top-of-the-table clash" in out


def test_json_includes_why():
    m = footy.score_match(event("h", "a"), _big_table(), LEAGUE, set())
    blob = json.dumps({
        "score": round(m["score"], 4), "derby": m["derby"],
        "why": m.get("why", []), "edge": m["edge"],
    })
    assert json.loads(blob)["why"] == m["why"]


def test_score_match_early_season_fade():
    # With two games played the table hasn't earned its confidence, so a
    # genuine top-of-the-table clash is rated more cautiously than it would be
    # late in the season, once the table signal has sharpened.
    def rows(played, pts_h, pts_a):
        t = {}
        t.update(table_row("h", "Home Big", 1, played=played, points=pts_h, gf=40, ga=10, won=14))
        t.update(table_row("a", "Away Big", 2, played=played, points=pts_a, gf=38, ga=12, won=13))
        return t

    early = footy.score_match(event("h", "a"), rows(2, 6, 3), LEAGUE, set())
    late = footy.score_match(event("h", "a"), rows(20, 45, 42), LEAGUE, set())
    assert late["score"] > early["score"]


def test_score_match_cross_border_outsider():
    # A tracked top side vs an unknown side is judged via domestic standing
    # and the unknown side treated as smaller.
    domestic = _big_table()
    known = footy.score_match(
        event("h", "unknown"), _big_table(), LEAGUE, set(),
        domestic=domestic, cross=True,
    )
    assert known is not None
    # unknown side scores lower than the same home side vs a known #2
    vs_known = footy.score_match(
        event("h", "a"), _big_table(), LEAGUE, set(),
        domestic=domestic, cross=True,
    )
    assert known["score"] < vs_known["score"]


def test_score_match_needs_two_competitors():
    one = event("h", "a")
    one["competitions"][0]["competitors"] = [
        {"homeAway": "home", "team": {"id": "h"}}
    ]
    assert footy.score_match(one, _big_table(), LEAGUE, set()) is None


# --------------------------------------------------------------------------
# integration: gather + render with fetch stubbed
# --------------------------------------------------------------------------

def _stub_fetch(monkeypatch, events_by_league, tables_by_league):
    def fake_fetch(url, cache_key=None, ttl=3600):
        for slug, evs in events_by_league.items():
            if cache_key and cache_key == f"fx_{slug}_20260904":
                return {"events": evs}
        for slug, rows in tables_by_league.items():
            if cache_key and cache_key == f"st_{slug}_2026":
                return {"entries": standings_entries(rows)}
        raise AssertionError(f"unexpected fetch: {cache_key or url}")

    monkeypatch.setattr(footy, "fetch", fake_fetch)
    footy._FIXTURES.clear()
    footy._TABLES.clear()
    footy._TEAMS.clear()


def test_gather_and_render(monkeypatch):
    from datetime import date

    eng_table = {}
    for i in range(1, 21):
        name = "Liverpool" if i == 1 else ("Everton" if i == 16 else f"Team{i}")
        eng_table.update(
            table_row(str(i), name, i, played=20, points=40 - i, gf=40, ga=10 + i)
        )

    events = [
        event("1", "16", "Liverpool", "Everton", state="pre"),
        event("99", "98", "Small A", "Small B", state="pre"),
    ]
    _stub_fetch(
        monkeypatch,
        {"eng.1": events},
        {"eng.1": eng_table},
    )

    cfg = footy.load_config()
    cfg["favourites"] = ["Liverpool"]
    cfg["leagues"] = {"eng.1": LEAGUE}
    matches = footy.gather(cfg, date(2026, 9, 4))
    assert len(matches) == 2
    # favourite match should score higher than two unknowns
    top = matches[0]
    assert top["favourite"] is True
    assert top["league"]["name"] == "Premier League"

    out = footy.render(matches, footy.timezone.utc, colour=False, show_date=date(2026, 9, 4))
    assert "Liverpool" in out
    assert "Everton" in out
    assert "<3" in out


# --------------------------------------------------------------------------
# nudge
# --------------------------------------------------------------------------

def _scored_event(home, away, iso, state="pre", score=0.5, fav=False, short="PL"):
    ev = event(home, away, state=state, date=iso)
    comp = ev["competitions"][0]
    return {
        "score": score,
        "favourite": fav,
        "home": comp["competitors"][0],
        "away": comp["competitors"][1],
        "event": ev,
        "league": {"name": "Premier League", "short": short, "weight": 1.0},
    }


def _nudge_cfg():
    return {"display": {"limit": 0, "min_stars": 0, "notify_top": 3, "nudge_min": 10},
            "leagues": {}}


def test_nudge_notifies_upcoming(monkeypatch):
    from datetime import date, datetime, timezone
    tz = timezone.utc
    fixed = datetime(2026, 9, 4, 18, 50, tzinfo=tz)
    matches = [
        _scored_event("h", "a", "2026-09-04T18:55:00Z", score=0.9),  # in window
        _scored_event("x", "y", "2026-09-04T20:00:00Z", score=0.8),  # far away
    ]
    monkeypatch.setattr(footy, "gather", lambda cfg, day, fixture_ttl=None: matches)
    sent = {}
    monkeypatch.setattr(footy, "_notify_send",
                        lambda title, body: sent.update({"title": title, "body": body}))
    cfg = _nudge_cfg()
    rc = footy.show_nudge(cfg, date(2026, 9, 4), tz, now=fixed)
    assert rc == 0
    assert "kickoff in 5m" in sent["title"]
    assert "Home" in sent["body"]


def test_nudge_includes_live_favourite(monkeypatch):
    from datetime import date, datetime, timezone
    tz = timezone.utc
    fixed = datetime(2026, 9, 4, 18, 50, tzinfo=tz)
    matches = [
        _scored_event("h", "a", "2026-09-04T18:30:00Z", state="in", score=0.5, fav=True),
    ]
    monkeypatch.setattr(footy, "gather", lambda cfg, day, fixture_ttl=None: matches)
    sent = {}
    monkeypatch.setattr(footy, "_notify_send",
                        lambda title, body: sent.update({"title": title, "body": body}))
    cfg = _nudge_cfg()
    rc = footy.show_nudge(cfg, date(2026, 9, 4), tz, now=fixed)
    assert rc == 0
    assert "live now" in sent["title"]
    assert " <3" in sent["body"]


def test_nudge_silent_when_nothing_near(monkeypatch):
    from datetime import date, datetime, timezone
    tz = timezone.utc
    fixed = datetime(2026, 9, 4, 18, 50, tzinfo=tz)
    matches = [_scored_event("h", "a", "2026-09-04T20:00:00Z", score=0.9)]
    monkeypatch.setattr(footy, "gather", lambda cfg, day, fixture_ttl=None: matches)
    sent = {}
    monkeypatch.setattr(footy, "_notify_send",
                        lambda title, body: sent.update({"title": title, "body": body}))
    cfg = _nudge_cfg()
    rc = footy.show_nudge(cfg, date(2026, 9, 4), tz, now=fixed)
    assert rc == 0
    assert not sent


def test_nudge_disabled_when_zero(monkeypatch):
    from datetime import date
    cfg = _nudge_cfg()
    cfg["display"]["nudge_min"] = 0
    monkeypatch.setattr(footy, "gather", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not gather")))
    assert footy.show_nudge(cfg, date(2026, 9, 4), footy.timezone.utc) == 0
