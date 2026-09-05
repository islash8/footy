#!/usr/bin/env bash
# Install footy: a `footy` command on PATH, plus an optional morning digest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${HOME}/.local/bin"
UNITS="${HOME}/.config/systemd/user"

mkdir -p "$BIN"
ln -sf "$ROOT/footy.py" "$BIN/footy"
echo "linked $BIN/footy -> $ROOT/footy.py"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on your PATH; add it in ~/.zshrc" ;;
esac

# --- man page -------------------------------------------------------------
MAN="${HOME}/.local/share/man/man1"
mkdir -p "$MAN"
install -m644 "$ROOT/man/footy.1" "$MAN/footy.1"
command -v mandb >/dev/null && mandb -q "${HOME}/.local/share/man" 2>/dev/null || true
echo "installed man page      -> man footy"

# --- tldr page ------------------------------------------------------------
# The Python tldr client reads pages straight out of its cache, and `tldr
# --update` writes entries individually rather than wiping the directory, so a
# page dropped in here survives updates. `tldr --clear-cache` removes it;
# re-run this script to put it back.
TLDR="${XDG_CACHE_HOME:-$HOME/.cache}/tldr/pages/common"
if command -v tldr >/dev/null; then
  mkdir -p "$TLDR"
  install -m644 "$ROOT/tldr/footy.md" "$TLDR/footy.md"
  echo "installed tldr page     -> tldr footy"
else
  echo "note: tldr not found; skipping its page"
fi

if [[ "${1:-}" == "--no-timer" ]]; then
  echo "skipping the morning digest timer"
  exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$UNITS"
  install -m644 "$ROOT/systemd/footy.service"        "$UNITS/footy.service"
  install -m644 "$ROOT/systemd/footy.timer"          "$UNITS/footy.timer"
  install -m644 "$ROOT/systemd/footy-kickoff.service" "$UNITS/footy-kickoff.service"
  install -m644 "$ROOT/systemd/footy-kickoff.timer"  "$UNITS/footy-kickoff.timer"
  if systemctl --user daemon-reload 2>/dev/null \
     && systemctl --user enable --now footy.timer footy-kickoff.timer 2>/dev/null; then
    echo "morning digest enabled:"
    systemctl --user list-timers footy.timer footy-kickoff.timer --no-pager | sed -n '1,3p'
    echo
    echo "change the digest time with: systemctl --user edit footy.timer"
    echo "turn the timers off with:    systemctl --user disable --now footy.timer footy-kickoff.timer"
  else
    echo "note: could not enable the user timers (no systemd user session?);"
    echo "      unit files are in $UNITS - enable with:"
    echo "      systemctl --user enable --now footy.timer footy-kickoff.timer"
  fi
else
  echo "note: systemctl not found; skipping the timers"
fi
