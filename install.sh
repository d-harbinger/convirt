#!/bin/sh
# Add convirt to the desktop's application menu for this user.
# It writes one file and copies nothing: the menu entry points at the
# convirt launcher beside this script, so the folder has to stay put.
#
#   sh install.sh             add the menu entry
#   sh install.sh --remove    remove it
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/convirt.desktop"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$ENTRY"
  echo "removed the menu entry"
  exit 0
fi

mkdir -p "$APPS"
cat > "$ENTRY" <<ENTRY_END
[Desktop Entry]
Type=Application
Name=convirt
Comment=Microphone, sound, 3D, display and network of a virtual machine
Exec="$HERE/convirt"
Icon=computer
Terminal=false
Categories=System;
ENTRY_END
echo "added convirt to the application menu"
