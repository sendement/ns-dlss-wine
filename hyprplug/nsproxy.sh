#!/bin/sh
# SPDX-License-Identifier: MIT
# nsproxy plugin helper for the REAL session: load | unload | status | reset. (Nested testing: see README, use hyprctl -i <sig>.)
P="$(cd "$(dirname "$0")" && pwd)/build/libnsproxy.so"
case "$1" in
  load) hyprctl plugin load "$P" ;;
  unload) hyprctl nsproxy detach; hyprctl plugin unload "$P" ;;
  status) hyprctl nsproxy status ;;
  reset) hyprctl nsproxy reset ;;
  *) echo "usage: $0 load|unload|status|reset"; exit 1 ;;
esac
