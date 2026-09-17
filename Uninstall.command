#!/bin/bash
pkill -f "Seating Plan.app/Contents/Resources/serve[.]py" 2>/dev/null && echo "Stopped the helper."
rm -rf "$HOME/Library/Application Support/SeatingPlanner"
[ -L "$HOME/Desktop/Seating Plan.app" ] && rm -f "$HOME/Desktop/Seating Plan.app" && echo "Removed the Desktop icon."
echo "Done. Delete this folder too if you want it fully gone."
