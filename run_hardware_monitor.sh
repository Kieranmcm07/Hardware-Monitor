#   Made by Kieranmcm07 on GitHub
#   GitHub: https://github.com/Kieranmcm07

#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
exec python3 -m hardware_monitor.gui
