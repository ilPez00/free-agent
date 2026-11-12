#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
chmod +x "$ROOT/bin/free-agent"
for target in "$HOME/.local/bin/free-agent" "$HOME/bin/free-agent"; do
  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$(readlink "$target" 2>/dev/null || true)" = "$ROOT/bin/free-agent" ]; then
      continue
    fi
    mv "$target" "$target.before-repo.$(date +%s)"
  fi
  ln -s "$ROOT/bin/free-agent" "$target"
done
printf 'Installed free-agent from %s\n' "$ROOT"
