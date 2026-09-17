# systemd 部署

每天北京时间 19:30 自动执行数据更新和审计。

```bash
sudo env WINSTOCK_USER=winstock ./deploy/install-systemd.sh
```

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
