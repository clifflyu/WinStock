# systemd 部署

每天北京时间 19:30 自动执行数据更新、审计、候选池生成，并推送一张飞书卡片。

**首次部署请照 [`飞书推送部署指南.md`](飞书推送部署指南.md) 操作**（含飞书机器人创建、
数据库准备、故障排查）。本文只说明脚本本身。

```bash
sudo ./deploy/install-systemd.sh
```

脚本会创建 `/etc/winstock/notify.env`（`0600 root:root`）存放飞书 Webhook。
**unit 文件以 0644 安装、全局可读**，所以密钥不能写进 unit 或 `ExecStart` 命令行
（`ps` 也全局可见），只能经 `EnvironmentFile=` 注入。重复运行安装脚本**不会覆盖**
已存在的 `notify.env`；如需在首次部署时一并写入，可设 `WINSTOCK_FEISHU_WEBHOOK`。

服务默认以当前用户运行。若使用 `WINSTOCK_USER` 指定专用用户，注意 `data/` 下已有的
库文件及其 `-wal`/`-shm` 也需一并 `chown`，否则服务写库会失败。

检查：

```bash
sudo systemctl start winstock-update.service
sudo systemctl status winstock-update.service
sudo systemctl list-timers winstock-update.timer --all
sudo journalctl -u winstock-update.service -n 100 --no-pager
```

停用：

```bash
sudo systemctl disable --now winstock-update.timer
```

脚本默认自动识别项目目录、Python 和 `data/winstock.db`。服务用户须能读取项目并写入数据库目录。
