#!/bin/bash
# Copyright (c) 2026 Dr Daniel Mompel Riera
# Licensed under the GNU Affero General Public License v3.0.
# Free to use and change; if you pass on a changed version, or let anyone
# use it over a network, you must publish your source under the same licence.
# Commercial use needs my permission: dmompelriera@nlcsjeju.kr
pkill -f "Seating Plan.app/Contents/Resources/serve[.]py" 2>/dev/null && echo "Stopped the helper."
rm -rf "$HOME/Library/Application Support/SeatingPlanner"
[ -L "$HOME/Desktop/Seating Plan.app" ] && rm -f "$HOME/Desktop/Seating Plan.app" && echo "Removed the Desktop icon."
echo "Done. Delete this folder too if you want it fully gone."
