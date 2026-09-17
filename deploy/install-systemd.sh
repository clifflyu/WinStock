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
tmp_morning=$(mktemp)
trap 'rm -f "$tmp_unit" "$tmp_morning"' EXIT
sed -e "s|__WINSTOCK_USER__|$user_escaped|g" \
    -e "s|__WINSTOCK_DIR__|$dir_escaped|g" \
    -e "s|__WINSTOCK_PYTHON__|$python_escaped|g" \
    -e "s|__WINSTOCK_DB__|$db_escaped|g" \
    "$deploy_dir/winstock-update.service.template" > "$tmp_unit"
sed -e "s|__WINSTOCK_USER__|$user_escaped|g" \
    -e "s|__WINSTOCK_DIR__|$dir_escaped|g" \
    -e "s|__WINSTOCK_PYTHON__|$python_escaped|g" \
    -e "s|__WINSTOCK_DB__|$db_escaped|g" \
    "$deploy_dir/winstock-morning.service.template" > "$tmp_morning"

install -m 0644 "$tmp_unit" /etc/systemd/system/winstock-update.service
install -m 0644 "$deploy_dir/winstock-update.timer" /etc/systemd/system/winstock-update.timer
install -m 0644 "$tmp_morning" /etc/systemd/system/winstock-morning.service
install -m 0644 "$deploy_dir/winstock-morning.timer" /etc/systemd/system/winstock-morning.timer

# ---- 飞书凭据 ----
# 配置放项目根目录的 .env。它不是 unit 文件的一部分，也不会出现在命令行里
# （ps 全局可见），所以只要保证不被提交即可——.gitignore 已单列 .env。
notify_env="$project_dir/.env"

if [[ ! -e "$notify_env" ]]; then
  # 只在首次创建，重复安装绝不覆盖已有的真实密钥。
  umask 077
  if [[ -f "$project_dir/.env.example" ]]; then
    cp "$project_dir/.env.example" "$notify_env"
  else
    printf 'WINSTOCK_FEISHU_WEBHOOK=\nWINSTOCK_FEISHU_SECRET=\n' > "$notify_env"
  fi
fi

# 允许在首次部署时一次性写入，省去手工编辑。
if [[ -n "${WINSTOCK_FEISHU_WEBHOOK:-}" ]]; then
  printf 'WINSTOCK_FEISHU_WEBHOOK=%s\nWINSTOCK_FEISHU_SECRET=%s\n' \
    "$WINSTOCK_FEISHU_WEBHOOK" "${WINSTOCK_FEISHU_SECRET:-}" > "$notify_env"
fi

chmod 0600 "$notify_env"

# 兜底自检：密钥一旦被提交就会永久留在 git 历史里，这里当场发现当场拦。
if ! git -C "$project_dir" check-ignore -q .env 2>/dev/null; then
  echo "⚠  警告：$project_dir/.env 未被 git 忽略！" >&2
  echo "   本仓库是公开的，提交它等于公开你的飞书 Webhook。请确认 .gitignore 含 .env。" >&2
fi

systemctl daemon-reload
systemctl enable --now winstock-update.timer winstock-morning.timer
systemctl list-timers winstock-update.timer winstock-morning.timer --all

echo
if grep -q '^WINSTOCK_FEISHU_WEBHOOK=.\+' "$notify_env"; then
  echo "飞书推送已配置。"
else
  echo "⚠  尚未配置飞书 Webhook：每日任务仍会更新数据，但推送会失败并以退出码 2 结束。"
  echo "   编辑 $notify_env 填入 WINSTOCK_FEISHU_WEBHOOK=... 即可。"
fi
echo "验证一次完整流程：sudo systemctl start winstock-update.service"
echo "查看日志：journalctl -u winstock-update.service -n 100 --no-pager"
