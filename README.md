# WinStock

A 股证券清单和前复权日 K 数据工具，数据保存在本地 SQLite。

要求 Python 3.10+，无需第三方依赖。

## 初始化

```bash
python3 -m winstocker init
python3 -m winstocker audit
```

默认数据库为 `data/winstock.db`，默认从 `2024-01-01` 开始下载。

## 常用命令

```bash
# 增量更新日 K
python3 -m winstocker update

# 检查数据库完整性
python3 -m winstocker audit

# 查看数据状态
python3 -m winstocker status
```

指定其他数据库：

```bash
python3 -m winstocker --db /path/to/winstock.db audit
```

## 定时部署

定时任务每天北京时间 19:30 自动执行 `update` 和 `audit`。

项目目录必须能被 `winstock` 用户读取，数据库目录必须能被该用户写入。

```bash
# 首次部署时创建服务用户和数据目录
id winstock >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin winstock
sudo install -d -o winstock -g winstock data

# 安装并启动定时器
sudo env WINSTOCK_USER=winstock ./deploy/install-systemd.sh
```

检查运行状态：

```bash
sudo systemctl start winstock-update.service
sudo systemctl status winstock-update.service
sudo systemctl list-timers winstock-update.timer --all
sudo journalctl -u winstock-update.service -n 100 --no-pager
```

停用定时任务：

```bash
sudo systemctl disable --now winstock-update.timer
```

安装脚本会自动识别项目目录、Python 和默认数据库路径。如需覆盖，可设置
`WINSTOCK_DIR`、`WINSTOCK_PYTHON` 或 `WINSTOCK_DB`。
