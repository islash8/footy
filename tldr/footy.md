# footy

> Ranks today's football matches by how interesting they are.
> Reads ESPN's public data - no API key, no ads, no trackers.
> More information: <https://github.com/islash8/footy>.

- Show today's matches, best first:

`footy`

- Show tomorrow, or the next seven days:

`footy {{-t|-w}}`

- Show a specific date:

`footy -d {{2026-11-07}}`

- Show only the top few, or only the strongest matches:

`footy {{-n 5|-s 4}}`

- Show the next matches for the teams you follow:

`footy -f`

- Show only what is being played right now:

`footy --live`

- Show the best matches of each competition, so smaller leagues aren't buried:

`footy -g`

- Show a league table:

`footy -T {{pl|laliga|champ|eredivisie}}`

- Send a desktop notification with today's top matches:

`footy --notify`

- Open the Nth match in your IPTV player, or pick interactively (optional;
  requires your own `[iptv]` setup in config.toml):

`footy --watch {{3}}`

`footy --watch`

- Copy your IPTV login from an iptvnator install into config.toml:

`footy --import-iptvnator`

- Print today's matches as an M3U for your IPTV player:

`footy --playlist`

- Output JSON, or keep colour when piping:

`footy {{--json|--color}}`
