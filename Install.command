#!/bin/bash
# Copyright (c) 2026 Dr Daniel Mompel Riera
# Licensed under the GNU Affero General Public License v3.0.
# Free to use and change; if you pass on a changed version, or let anyone
# use it over a network, you must publish your source under the same licence.
# Commercial use needs my permission: dmompelriera@nlcsjeju.kr
# Double-click to put Seating Plan on this Mac - or to update the copy already on it.
#
# An update replaces the app and nothing else. Your classes, your rooms and which class
# was open are not inside the app: they sit beside it, in Classes, Rooms.json and
# active_class. This script never opens them, never moves them and never writes to them.
# It counts them before it starts and again when it has finished, and shows you both
# numbers, so you are not asked to take that on trust.
#
# The version being replaced is kept as "Seating Plan (previous version).app" next to the
# new one. If anything is wrong, delete the new one and take the ".app" off the end of the
# old one's name.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
NEW="$HERE/Seating Plan.app"

note(){ printf '%s\n' "$*"; }
ask(){                                   # $1 message  $2 left button  $3 right button
  # A school pushing this out to many Macs can set SEATING_PLAN_UNATTENDED=1 and get the
  # right-hand answer without anybody clicking anything.
  [ "${SEATING_PLAN_UNATTENDED:-0}" = "1" ] && { printf '%s' "$3"; return; }
  osascript -e "button returned of (display dialog \"$1\" buttons {\"$2\", \"$3\"} \
    default button \"$3\" with title \"Seating Plan\")" 2>/dev/null
}
tell(){ [ "${SEATING_PLAN_UNATTENDED:-0}" = "1" ] && return; osascript -e "display alert \"Seating Plan\" message \"$1\"" >/dev/null 2>&1; }

# ---------------------------------------------------------- is this copy fit to install?
[ -d "$NEW" ] || { note "There is no Seating Plan.app in this folder."; exit 1; }
for f in "Contents/Info.plist" "Contents/MacOS/SeatingPlan" "Contents/MacOS/app-window" \
         "Contents/Resources/plan.html" "Contents/Resources/serve.py" \
         "Contents/Resources/import.py"; do
  [ -e "$NEW/$f" ] || {
    note "This copy is incomplete - $f is missing. Nothing has been changed."
    tell "This download looks incomplete, so nothing was changed. Download it again."
    exit 1; }
done

# ------------------------------------------------- what does a teacher's own work look like
# Everything a teacher has made lives in three places, all of them outside the app:
# Classes (one folder each, holding the class list, the photographs, and plan.json - which
# is where the room you drew and where everybody sits are kept), Rooms.json (rooms you
# saved to reuse), and active_class. This script reads them to describe and to check them,
# and writes to none of them.

describe(){                              # $1 = install folder -> a line a teacher can read
  local d="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$d" <<'PYEOF' 2>/dev/null && return
import json, os, sys, csv, glob
d = sys.argv[1]
cls = sorted(g for g in glob.glob(os.path.join(d, 'Classes', '*')) if os.path.isdir(g))
students = photos = plans = pieces = 0
for c in cls:
    f = os.path.join(c, 'students.csv')
    if os.path.isfile(f):
        with open(f, newline='') as fh: students += max(0, sum(1 for _ in csv.reader(fh)) - 1)
    pd = os.path.join(c, 'photos')
    if os.path.isdir(pd): photos += len([x for x in os.listdir(pd) if not x.startswith('.')])
    p = os.path.join(c, 'plan.json')
    if os.path.isfile(p):
        try:
            j = json.load(open(p))
            pl = j.get('plans') or []
            plans += len(pl)
            for one in pl: pieces += len(((one.get('layout') or {}).get('items')) or [])
        except Exception: pass
rooms = 0
rj = os.path.join(d, 'Rooms.json')
if os.path.isfile(rj):
    try:
        r = json.load(open(rj))
        rooms = len(r) if isinstance(r, list) else len(r or {})
    except Exception: pass
print('%d classes, %d students, %d photographs, %d seating plans '
      '(%d pieces of furniture) and %d saved rooms'
      % (len(cls), students, photos, plans, pieces, rooms))
PYEOF
  fi
  local c=0
  [ -d "$d/Classes" ] && c=$(find "$d/Classes" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')
  printf '%s classes and everything in them' "$c"
}

digest(){                                # every byte of it, so nothing has to be taken on trust
  local d="$1"
  { find "$d/Classes" "$d/Rooms.json" "$d/active_class" -type f 2>/dev/null | LC_ALL=C sort
    find "$d/Classes" "$d/Rooms.json" "$d/active_class" -type f 2>/dev/null | LC_ALL=C sort \
      | tr '\n' '\0' | xargs -0 md5 -q 2>/dev/null
  } | md5 -q
}
hasWork(){ [ -d "$1/Classes" ] || [ -f "$1/Rooms.json" ]; }

# ------------------------------------------------------ find the copy already on this Mac
FOUND=""
try(){
  local d="$1"
  [ -n "$d" ] && [ -d "$d/Seating Plan.app" ] && [ "$d" != "$HERE" ] && hasWork "$d" \
    && [ -z "$FOUND" ] && FOUND="$d"
}
# the Desktop icon points straight at it, for anyone who has ever run this script
if [ -L "$HOME/Desktop/Seating Plan.app" ]; then
  T="$(readlink "$HOME/Desktop/Seating Plan.app")"
  case "$T" in /*) ;; *) T="$HOME/Desktop/$T" ;; esac
  try "$(cd "$(dirname "$T")" 2>/dev/null && pwd)"
fi
try "/Applications/SeatingPlan"
try "$HOME/Applications/SeatingPlan"
try "$HOME/Desktop/SeatingPlan"
try "$HOME/Documents/SeatingPlan"

# ============================================================================== UPDATING
if [ -n "$FOUND" ]; then
  WHAT="$(describe "$FOUND")"
  BEFORE="$(digest "$FOUND")"
  A="$(ask "Seating Plan is already on this Mac, at:\n$FOUND\n\nIt holds $WHAT.\n\nUpdate the app there? Your classes and rooms are not touched - they are kept outside the app, and this only replaces the app itself." "Not now" "Update it")"
  [ "$A" = "Update it" ] || { note "Nothing was changed."; exit 0; }

  note "Updating the app at: $FOUND"
  note "Leaving alone:       $WHAT"

  # Close it first, or it carries on running the old version.
  osascript -e 'tell application "Seating Plan" to quit' >/dev/null 2>&1
  pkill -f "Seating Plan.app/Contents/MacOS/Seating Plan" >/dev/null 2>&1
  pkill -f "Contents/Resources/serve.py" >/dev/null 2>&1
  sleep 1

  STAGE="$FOUND/Seating Plan.app.incoming"
  OLD="$FOUND/Seating Plan (previous version).app"
  rm -rf "$STAGE"
  if ! cp -R "$NEW" "$STAGE"; then
    note "The new app could not be copied in. Nothing has been changed."
    tell "The update could not be copied in, so nothing was changed."
    exit 1
  fi
  rm -rf "$OLD"
  mv "$FOUND/Seating Plan.app" "$OLD" && mv "$STAGE" "$FOUND/Seating Plan.app" || {
    # put it back the way it was
    [ -d "$OLD" ] && [ ! -d "$FOUND/Seating Plan.app" ] && mv "$OLD" "$FOUND/Seating Plan.app"
    rm -rf "$STAGE"
    note "The swap failed, so the old app has been left in place."
    tell "The update did not complete. Your old app and all your classes are still there."
    exit 1
  }
  [ -f "$HERE/READ ME FIRST.html" ] && cp -f "$HERE/READ ME FIRST.html" "$FOUND/" 2>/dev/null
  [ -f "$HERE/Uninstall.command" ]  && cp -f "$HERE/Uninstall.command"  "$FOUND/" 2>/dev/null
  [ -f "$HERE/Install.command" ]    && cp -f "$HERE/Install.command"    "$FOUND/" 2>/dev/null
  chmod +x "$FOUND/Install.command" "$FOUND/Uninstall.command" 2>/dev/null
  chmod -R +x "$FOUND/Seating Plan.app/Contents/MacOS" 2>/dev/null
  xattr -dr com.apple.quarantine "$FOUND" 2>/dev/null
  # the window app is compiled from main.swift and cached; drop it so the new one is built
  rm -rf "$HOME/Library/Application Support/SeatingPlanner/bin" 2>/dev/null
  rm -f  "$HOME/Library/Application Support/SeatingPlanner/server.url" 2>/dev/null

  AFTER="$(digest "$FOUND")"
  note "Yours, read again:   $(describe "$FOUND")"
  if [ "$BEFORE" = "$AFTER" ]; then
    note "Not one byte of it changed. The version replaced is kept as \"Seating Plan (previous version).app\"."
  else
    note "SOMETHING IN YOUR FOLDER CHANGED. This script only ever wrote to the app itself."
    tell "The update finished, but the check on your classes and rooms did not match. Nothing in them was written to by the updater. Please look at $FOUND before carrying on."
  fi

  LINK="$HOME/Desktop/Seating Plan.app"
  if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
    rm -f "$LINK"; ln -s "$FOUND/Seating Plan.app" "$LINK"
  fi
  note "Opening it..."
  "$FOUND/Seating Plan.app/Contents/MacOS/SeatingPlan" >/dev/null 2>&1 &
  tell "Updated. Your classes, rooms and seating plans are exactly as they were. You can throw this download away now."
  note "You can close this Terminal window, and throw this downloaded folder away."
  exit 0
fi

# ========================================================================= FIRST INSTALL
# The installer does the installing. Nobody should ever drag this folder into Applications
# by hand: if a folder of the same name is already there, the Finder's "Replace" throws the
# old one away - and the old one is where every class, photograph and seating plan lives.
# That is why this folder is not called what the installed one is called, and why it puts
# itself in place instead of being dragged.
TARGET="/Applications/SeatingPlan"
if [ "$HERE" = "$TARGET" ]; then
  INSTALLED="$HERE"                       # already in the right place; just set it up
else
  if ! mkdir -p "$TARGET" 2>/dev/null; then
    TARGET="$HOME/Applications/SeatingPlan"
    mkdir -p "$TARGET" || { note "Could not create $TARGET."; tell "Seating Plan could not be installed - $TARGET could not be created."; exit 1; }
  fi
  note "Installing into: $TARGET"
  cp -R "$HERE/Seating Plan.app" "$TARGET/" || { note "Could not copy the app in."; exit 1; }
  for f in "READ ME FIRST.html" "Install.command" "Uninstall.command"; do
    [ -f "$HERE/$f" ] && cp -f "$HERE/$f" "$TARGET/" 2>/dev/null
  done
  chmod +x "$TARGET/Install.command" "$TARGET/Uninstall.command" 2>/dev/null
  INSTALLED="$TARGET"
fi

chmod -R +x "$INSTALLED/Seating Plan.app/Contents/MacOS" 2>/dev/null
xattr -dr com.apple.quarantine "$INSTALLED" 2>/dev/null
touch "$INSTALLED/Seating Plan.app"
LINK="$HOME/Desktop/Seating Plan.app"
if [ -L "$LINK" ] || [ ! -e "$LINK" ]; then
  rm -f "$LINK"; ln -s "$INSTALLED/Seating Plan.app" "$LINK" && note "Desktop icon: Seating Plan"
else
  note "Left the existing $LINK alone."
fi

# If macOS is still holding files back - after copying to another Mac, say - point the
# way through rather than leaving a dead icon behind.
if xattr -pr com.apple.quarantine "$INSTALLED" 2>/dev/null | grep -q .; then
  note ""
  note "Some files are still blocked by macOS. Opening the settings page for you."
  A="$(ask "macOS is still holding some of these files back.\n\nIn System Settings, scroll to the bottom of Privacy & Security and click Open Anyway." "Not now" "Open Settings")"
  [ "$A" = "Open Settings" ] && open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
fi

note ""
note "Opening it so you can see it working..."
"$INSTALLED/Seating Plan.app/Contents/MacOS/SeatingPlan" >/dev/null 2>&1 &
note "Installed at: $INSTALLED"
note "You can close this Terminal window, and throw this downloaded folder away."
tell "Seating Plan is installed at $INSTALLED, and there is an icon on your Desktop. You can throw the download away."
