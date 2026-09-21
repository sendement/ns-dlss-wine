#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Clones the optional modules listed in tools/modules.list (or given as `name url [ref]` arguments) into modules/<name> and builds them when they ship a build.sh.
# Modules are separate programs under their own licenses (see docs/module-protocol.md); nothing of them is part of this repository.
#   tools/fetch_modules.sh                       # everything in tools/modules.list
#   tools/fetch_modules.sh mako https://github.com/<owner>/ns-mako
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; mkdir -p "$ROOT/modules"
fetch() {
  local name="$1" url="$2" ref="${3:-}"
  if [ ! -d "$ROOT/modules/$name/.git" ]; then git clone "$url" "$ROOT/modules/$name"; else git -C "$ROOT/modules/$name" pull --ff-only; fi
  [ -n "$ref" ] && git -C "$ROOT/modules/$name" checkout --quiet "$ref"
  if [ -x "$ROOT/modules/$name/build.sh" ]; then echo "building module $name ..."; (cd "$ROOT/modules/$name" && ./build.sh); fi
}
if [ "$#" -ge 2 ]; then fetch "$@"; else
  while read -r name url ref; do
    case "$name" in ''|'#'*) continue;; esac
    fetch "$name" "$url" "$ref"
  done < "$ROOT/tools/modules.list"
fi
python3 "$ROOT/app/modules.py"
