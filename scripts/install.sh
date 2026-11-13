#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
chmod +x "$ROOT/bin/free-agent" "$ROOT/bin/free-agent-configure"
for target in "$HOME/.local/bin/free-agent" "$HOME/bin/free-agent" \
              "$HOME/.local/bin/free-agent-configure" "$HOME/bin/free-agent-configure"; do
  src="$ROOT/bin/$(basename "$target")"
  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ "$(readlink "$target" 2>/dev/null || true)" = "$src" ]; then
      continue
    fi
    mv "$target" "$target.before-repo.$(date +%s)"
  fi
  ln -s "$src" "$target"
done
printf 'Installed free-agent (+ configurator) from %s\n' "$ROOT"
