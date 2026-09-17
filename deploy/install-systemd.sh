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

# ---- 飞书凭据 ----
# unit 文件以 0644 安装、全局可读，所以 Webhook 绝不能写进 unit 或 ExecStart 命令行
# （ps 也全局可见）。只能经 EnvironmentFile 注入，文件本身锁到 0600。
notify_dir=/etc/winstock
notify_env="$notify_dir/notify.env"

install -d -m 0750 -o root -g root "$notify_dir"

if [[ ! -e "$notify_env" ]]; then
  # 只在首次创建，重复安装绝不覆盖已有的真实密钥。
  umask 077
  cat > "$notify_env" <<'ENVEOF'
# WinStock 飞书播报配置。此文件含密钥，请勿提交到版本库，勿改为 644。
# 注意：systemd 不解析引号，值两侧不要加 " 或 '。
WINSTOCK_FEISHU_WEBHOOK=
WINSTOCK_FEISHU_SECRET=
ENVEOF
fi

# 允许在首次部署时一次性写入，省去手工编辑。
if [[ -n "${WINSTOCK_FEISHU_WEBHOOK:-}" ]]; then
  printf 'WINSTOCK_FEISHU_WEBHOOK=%s\nWINSTOCK_FEISHU_SECRET=%s\n' \
    "$WINSTOCK_FEISHU_WEBHOOK" "${WINSTOCK_FEISHU_SECRET:-}" > "$notify_env"
fi

chmod 0600 "$notify_env"
chown root:root "$notify_env"

systemctl daemon-reload
systemctl enable --now winstock-update.timer
systemctl list-timers winstock-update.timer --all

echo
if grep -q '^WINSTOCK_FEISHU_WEBHOOK=.\+' "$notify_env"; then
  echo "飞书推送已配置。"
else
  echo "⚠  尚未配置飞书 Webhook：每日任务仍会更新数据，但推送会失败并以退出码 2 结束。"
  echo "   编辑 $notify_env 填入 WINSTOCK_FEISHU_WEBHOOK=... 即可。"
fi
echo "验证一次完整流程：sudo systemctl start winstock-update.service"
echo "查看日志：journalctl -u winstock-update.service -n 100 --no-pager"
