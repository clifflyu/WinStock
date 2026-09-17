# systemd 部署

交易日北京时间 09:46 执行前夜计划的模拟成交并推送；每天 19:30 执行数据更新、
审计、候选池生成、收盘估值、次日计划并推送。09:46 是为了等待 09:45 的首根15分钟K完整形成。

**首次部署请照 [`飞书推送部署指南.md`](飞书推送部署指南.md) 操作**（含飞书机器人创建、
数据库准备、故障排查）。本文只说明脚本本身。

```bash
sudo ./deploy/install-systemd.sh
```

脚本会在**项目根目录**创建 `.env`（`0600`，内容取自 `.env.example`）存放飞书
Webhook，并检查它确实被 `.gitignore` 忽略（本仓库公开，提交它等于公开凭据）。unit 文件以 0644 安装、
全局可读，所以密钥不能写进 unit 或 `ExecStart` 命令行（`ps` 也全局可见）；
`.env` 由程序自行读取，不经 systemd。重复运行安装脚本**不会覆盖**已存在的
`.env`；如需在首次部署时一并写入，可设 `WINSTOCK_FEISHU_WEBHOOK`。

服务默认以当前用户运行。若使用 `WINSTOCK_USER` 指定专用用户，注意 `data/` 下已有的
库文件及其 `-wal`/`-shm` 也需一并 `chown`，否则服务写库会失败。

检查：

```bash
sudo systemctl start winstock-update.service
sudo systemctl status winstock-update.service
sudo systemctl list-timers winstock-update.timer winstock-morning.timer --all
sudo journalctl -u winstock-update.service -n 100 --no-pager
sudo journalctl -u winstock-morning.service -n 100 --no-pager
```

停用：

```bash
sudo systemctl disable --now winstock-update.timer winstock-morning.timer
```

脚本默认自动识别项目目录、Python 和 `data/winstock.db`。服务用户须能读取项目并写入数据库目录。
