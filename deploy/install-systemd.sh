#!/usr/bin/env bash
# Install the supplied systemd timer. Run this script on the target server with sudo.
set -euo pipefail

deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_dir=${WINSTOCK_DIR:-$(cd -- "$deploy_dir/.." && pwd)}
run_user=${WINSTOCK_USER:-$(id -un)}
python_bin=${WINSTOCK_PYTHON:-}
db_path=${WINSTOCK_DB:-"$project_dir/data/winstock.db"}

if [[ -z "$python_bin" ]]; then
  if [[ -x "$project_dir/.venv/bin/python" ]]; then
    python_bin="$project_dir/.venv/bin/python"
  else
    python_bin=$(command -v python3)
  fi
fi

[[ -x "$python_bin" ]] || { echo "Python executable not found: $python_bin" >&2; exit 1; }
[[ -d "$project_dir/winstocker" ]] || { echo "WinStock project not found: $project_dir" >&2; exit 1; }

escape_sed() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }
user_escaped=$(escape_sed "$run_user")
dir_escaped=$(escape_sed "$project_dir")
python_escaped=$(escape_sed "$python_bin")
db_escaped=$(escape_sed "$db_path")

tmp_unit=$(mktemp)
trap 'rm -f "$tmp_unit"' EXIT
sed -e "s|__WINSTOCK_USER__|$user_escaped|g" \
    -e "s|__WINSTOCK_DIR__|$dir_escaped|g" \
    -e "s|__WINSTOCK_PYTHON__|$python_escaped|g" \
    -e "s|__WINSTOCK_DB__|$db_escaped|g" \
    "$deploy_dir/winstock-update.service.template" > "$tmp_unit"

install -m 0644 "$tmp_unit" /etc/systemd/system/winstock-update.service
install -m 0644 "$deploy_dir/winstock-update.timer" /etc/systemd/system/winstock-update.timer
systemctl daemon-reload
systemctl enable --now winstock-update.timer
systemctl list-timers winstock-update.timer --all

echo "Installed. Test once with: sudo systemctl start winstock-update.service"
echo "View logs with: journalctl -u winstock-update.service -n 100 --no-pager"
