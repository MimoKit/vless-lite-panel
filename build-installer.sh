#!/usr/bin/env bash
set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${root_dir}/install.template.sh"
panel="${root_dir}/panel.py"
output="${root_dir}/install.sh"

panel_b64="$(base64 -w 0 "$panel" 2>/dev/null || base64 "$panel" | tr -d '\n')"

python3 - "$template" "$output" "$panel_b64" <<'PY'
from pathlib import Path
import sys

template = Path(sys.argv[1]).read_text(encoding="utf-8")
output = Path(sys.argv[2])
payload = sys.argv[3]
marker = "__PANEL_B64__"
if template.count(marker) != 1:
    raise SystemExit("installer template marker is missing or duplicated")
output.write_text(template.replace(marker, payload), encoding="utf-8")
output.chmod(0o755)
PY

printf 'Built %s\n' "$output"
