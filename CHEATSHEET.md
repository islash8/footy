# footy — cheatsheet

## Looking it up later

```sh
tldr footy      # the examples
man footy       # the full reference
footy --help    # the flags
```

## The one you'll use

```sh
footy            # today, ranked best first
```

## Everything else

| Command | Does |
|---|---|
| `footy` | Today, every fixture, ranked |
| `footy -t` | Tomorrow |
| `footy -w` | Next 7 days |
| `footy -d 2026-11-07` | A specific date |
| `footy -n 5` | Top 5 only |
| `footy -s 4` | Only 4-star and above |
| `footy -f` | Next matches for **your** teams, up to 3 weeks out |
| `footy --live` | Only what's kicking about right now, with scores |
| `footy -g` | Best of *each* competition (nothing buried) |
| `footy -T` | Every league table |
| `footy -T pl` | One table — `pl`, `laliga`, `champ`, `2.bl`, `eredivisie`… |
| `footy --json` | Machine-readable |
| `footy --notify` | Fire the desktop notification now |
| `footy --watch 3` | Open match #3 in your IPTV player (mpv/vlc) |
| `footy --watch` | Numbered list, then pick one to open |
| `footy --playlist` | Today's matches as an M3U, for your player |
| `footy --iptv-channels` | List provider channels, grouped by name/quality |
| `footy --color` | Force colour when piping (e.g. `footy --color \| less -R`) |

Combine freely: `footy -d 2026-11-07 -g -n 1`

## Reading a line

```
  ****.  17:00  C Palace 18 (H) v Liverpool 13 (A)  PL <3
                LLWLD / DDDLD
```

Team names are coloured by who's expected to win:

| Colour | Meaning |
|---|---|
| **green** | Favourite — table and form say they win |
| **amber** | Underdog |
| **purple** | Too close to call |

Worked out from league position, last-five form and a nod to home advantage.
Early in a season the bar for calling a favourite is deliberately higher, so
more matches show purple until the table has earned its confidence.

Note the two senses of "favourite": the `<3` is *your* team (Liverpool), the
green is the *match* favourite. They're unrelated — Liverpool often shows
`<3` in amber.

Kitty gets 24-bit colour; it falls back to 256 then basic ANSI elsewhere, and
turns itself off when piped unless you pass `--color`.

| Bit | Meaning |
|---|---|
| `****.` | 4 of 5 stars — **absolute**, so a dull day looks dull |
| `17:00` | Kickoff, your local time. `LIVE` or `FT` once underway |
| `18` / `13` | League position |
| `(H)` `(A)` | Home / away |
| `PL` | Competition |
| `<3` | A team you follow |
| colour | Green favourite, amber underdog, purple too close |
| `LLWLD` | Last five results, home / away |

## Config

`config.toml`, next to `footy.py` — edit and re-run, nothing to reload.
Copy `config.example.toml` to `config.toml` to start.

```toml
favourites = []                 # e.g. "Liverpool" - boosted, not pinned

[display]
limit = 0                       # 0 = show all
min_stars = 0
notify_top = 3

[leagues."ned.1"]
name = "Eredivisie"
short = "Eredivisie"
weight = 0.82                   # higher = surfaces more
```

Add any ESPN slug: `esp.2` `ita.2` `por.1` `sco.1` `tur.1` `nor.1` `swe.1`
`bra.1` `arg.1` `mex.1` `usa.1` `jpn.1` `ned.2`.

## Watching a match (optional, BYO-IPTV)

`footy --watch 3` opens match #3 in mpv/vlc. Only active when `config.toml`
has your own `[iptv]` block — nothing IPTV ships with footy itself. Set it
up once:

```sh
footy --import-iptvnator   # copies your Xtream login from iptvnator
# or add [iptv] by hand (see config.example.toml)
```

footy picks the channel from the beIN MENA EPG (`[iptv] epg_url`, default
al7omed/bein-epg) by matching the kickoff window + team names, then matches
the EPG channel to a provider channel by name and takes the best quality.
Anything unmatched falls back to an interactive picker that remembers your
last pick per league.

```toml
[iptv]
host = "http://provider.example:2095"
username = "user"
password = "pass"
player = "mpv"                    # or vlc
quality = ["4K", "FHD", "HD", "SD", "LOW"]
# epg_url = "https://al7omed.github.io/bein-epg/guide.xml"   # configurable

[iptv.channels]                    # optional picker hints
# "uefa.champions" = "beIN SPORTS 1"
```

## Morning digest

Fires 09:00 daily with the top 3.

```sh
systemctl --user edit footy.timer        # change the time
systemctl --user disable --now footy.timer   # stop it
systemctl --user start footy.service     # test it now
```

## How ranking works

Score = league weight × (table position `0.42` + both sides strong `0.15`
+ tight table gap `0.12` + form `0.18–0.48` + likely goals `0.20`
+ derby `0.22`) + `0.30` if it's your team.

Two things that trip people up:

- **Early season, the table is discounted.** Below 8 games played it fades
  toward neutral and form takes over — so stars sharpen as the season runs.
- **European ties use domestic position.** "Real Madrid, 27th" in a 36-team
  league phase is meaningless. Clubs outside your tracked leagues (Club
  Brugge, Viking FK) show no number and are treated as smaller.

## Gotchas

- Standings are always **current** — asking for a future date still uses
  today's table.
- Works offline from cache (`~/.cache/footy`, fixtures 5 min, tables 6 h).
- Data is ESPN's public API. No key, no signup, no ads, no trackers.
