# footy

One command, the football worth watching. No API key, no signup, no ads,
no cookie banners, no trackers.

```
$ footy

  Wednesday 21 October

  *****  21:00        Bayern v Arsenal  UCL
                1st v 2nd  WWWWW / WWWWL
  *****  21:00   Real Madrid v RB Leipzig  UCL
                2nd v 4th  WWWWW / WWLWL
  ****.  21:00   Club Brugge v Lens  UCL
                Lens 7th  LWWWW / LWWWW
  **...  21:00   Aston Villa v Viking FK  UCL
                LLLWW / WWDLW
```

## Install

```sh
./install.sh              # `footy` on PATH + 09:00 desktop digest
./install.sh --no-timer   # just the command
```

Requires Python 3.11+ (3.14 here) and nothing else — standard library only.

## Documentation

```sh
tldr footy      # examples, the way you'd look up any other command
man footy       # full reference
footy --help    # flags
```

Both pages are installed by `install.sh` from `tldr/footy.md` and `man/footy.1`.
The tldr page lives in the client's cache and survives `tldr --update`; only
`tldr --clear-cache` removes it, and re-running `install.sh` puts it back.

## Usage

```sh
footy                  # today
footy -t               # tomorrow
footy -w               # the next 7 days
footy -d 2026-10-21    # a specific date
footy -n 5             # top 5 only
footy -s 4             # only 4-star matches and up
footy -f               # next matches for the teams you follow
footy --live           # only what's being played right now
footy -g               # best of each competition, so small leagues aren't buried
footy -T               # every league table
footy -T pl            # one table (pl, laliga, champ, 2.bl, eredivisie ...)
footy --color          # force colour when piping into less -R
footy --json           # machine-readable
footy --notify         # desktop notification with the top 3
footy --watch 3        # open match #3 in your IPTV player (mpv/vlc)
footy --watch          # numbered list, then pick one
footy --playlist       # today's matches as an M3U, for importing into a player
footy --iptv-channels  # list your provider's channels, grouped by name/quality
footy --import-iptvnator  # one-shot: copy your IPTV login from iptvnator
footy --version
```

See [CHEATSHEET.md](CHEATSHEET.md) for the quick reference.

## Watching a match (optional)

`footy --watch N` opens the N-th match from the day's ranking in a local
player. **This is fully optional and bring-your-own:** it only activates
when your `config.toml` has an `[iptv]` section with your own IPTV provider
credentials. Nothing IPTV-related ships in the repo, and footy's ranking
works the same without it.

Set it up once, either way:

```sh
footy --import-iptvnator   # if you use iptvnator: copies your Xtream login in one shot
# or add [iptv] by hand (see config.example.toml): host / username / password
```

`-n` and `-s` behave as usual with `--watch`, so the numbering matches what
`footy` prints. Interactive mode annotates each line with the channel the
EPG expects, so you can see the assignment before choosing.

How the channel is chosen:

1. footy reads a public beIN MENA EPG (`https://al7omed.github.io/bein-epg/guide.xml`,
   scraped from beinsports.com, regenerated every 12h) and looks for a
   programme around the match's kickoff that names both teams — so "Liverpool
   is on beIN 2 this week, beIN 3 next" is answered from the broadcaster's own
   schedule, not a guess.
2. The EPG channel is matched to a provider channel by name (`beIN SPORTS 1`
   ↔ `beIN_SPORTS1_4K`), and the best available quality is picked from your
   `quality` list.
3. The stream opens in `mpv` (or `vlc`, or anything set as `player` in
   `[iptv]`).

Anything the EPG can't name (leagues it doesn't cover, off-air slots, an
unreachable feed) falls back to an interactive channel picker, which
remembers your last choice per league in `~/.cache/footy/watchstate.json`.

The EPG URL is a config value — swap in another XMLTV source in `[iptv]`
(`epg_url = "..."`) if you ever need to.

## Reading a line

```
  ****.  17:00  C Palace 18 (H) v Liverpool 13 (A)  PL <3
                LLWLD / DDDLD
```

Stars (absolute, 1-5), kickoff in local time, league position beside each
club, `(H)`/`(A)` for home and away, the competition, `<3` for a team you
follow, and both sides' last five results.

Team names are coloured by expectation: **green** for the favourite, **amber**
for the underdog, **purple** when it's too close to call - drawn from table
position, form and home advantage. Don't confuse the two senses of the word:
`<3` marks *your* team, the colour marks the *match* favourite.

Truecolor on kitty and similar, 256-colour or basic ANSI elsewhere, and off
when piped unless you pass `--color`.

## How matches are ranked

Every fixture gets a score from five signals, then a 1–5 star rating on an
**absolute** scale — a thin Tuesday genuinely looks thin.

| Signal | Weight | Notes |
|---|---|---|
| League position of both sides | 0.42 | How high up the table they are |
| Both sides strong | +0.15 | A real top-of-the-table clash |
| Small gap in the table | +0.12 | Six-pointers |
| Recent form | 0.18–0.48 | Last five results |
| Likely goals | +0.20 | How many goals both sides are involved in |
| Rivalry | +0.22 | 14 curated derbies |
| A team you follow | +0.30 | A boost, not a pin |

Two details that matter:

- **Early-season tables are noise.** After two games, "18th" means nothing.
  The table signal fades toward neutral below eight games played, and form
  takes up the slack.
- **European ties are judged on domestic standing.** In a 36-team league
  phase, Real Madrid sitting 27th tells you nothing. Clubs are sized up by
  their league position at home; sides outside the tracked leagues are
  treated as smaller.

## Configuration

Everything lives in `config.toml` — teams you follow, which competitions to
cover and how much each is worth. Edit and re-run; there is nothing to reload.
`config.toml` is gitignored; start from `config.example.toml`:

```sh
cp config.example.toml config.toml
```

Tracked by default: Premier League, LaLiga, Serie A, Bundesliga, Ligue 1,
Champions League, Europa League, Championship, Eredivisie and 2. Bundesliga.

Any [ESPN soccer slug](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard)
works. Verified: `eng.1` `eng.2` `esp.1` `esp.2` `ita.1` `ita.2` `ger.1` `ger.2`
`fra.1` `ned.1` `ned.2` `por.1` `sco.1` `tur.1` `nor.1` `swe.1` `arg.1` `bra.1`
`mex.1` `usa.1` `jpn.1` `uefa.champions` `uefa.europa`.

Note that a big league buries a small one in the single global ranking, which
is why `-g` exists. Weights for Eredivisie (0.82) and 2. Bundesliga (0.76) are
set above their raw prestige deliberately, because they were added for
entertainment rather than stature.

## Data

ESPN's public JSON API. Responses are cached under `~/.cache/footy`
(5 min for fixtures, 6 h for tables), and a stale cache is served if the
network is down, so the command still answers offline.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/pytest -q        # test suite; network-free, fetch is stubbed
```
