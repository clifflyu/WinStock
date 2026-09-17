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

## 每日飞书播报

定时任务每天北京时间 19:30 自动执行 `daily`：更新日 K → 审计数据 → 生成候选池 →
推送一张飞书卡片到群里。正常情况下你不需要执行任何命令，看飞书即可。

```bash
# 只发一条配置自检卡片，验证飞书链路（秒级，不碰数据）
python3 -m winstocker notify --test

# 预览卡片内容但不发送
python3 -m winstocker daily --no-update --dry-run

# 完整跑一次（含数据更新）
python3 -m winstocker daily
```

Webhook 地址是凭据，配置写在**项目根目录的 `.env`**（已在 `.gitignore` 中，本仓库
是公开的）：

```
WINSTOCK_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
WINSTOCK_FEISHU_SECRET=          # 仅当机器人开启签名校验时填写
```

读取优先级为**命令行参数 > 环境变量 > `.env`**，环境变量名为
`WINSTOCK_FEISHU_WEBHOOK` / `WINSTOCK_FEISHU_SECRET`。
**完整部署步骤见 [`deploy/飞书推送部署指南.md`](deploy/飞书推送部署指南.md)。**

`daily` 无论中间哪一步失败都会照常推送——用户看到沉默时无法区分「今天没事」和
「整条链路已经死了」。退出码区分失败面：`0` 全部成功；`1` 数据更新失败但红色卡片
已送达；`2` 数据正常但卡片发送失败；`3` 两者都失败。

## 定时部署

定时任务每天北京时间 19:30 自动执行 `daily`。**首次部署请直接用
[`deploy/飞书推送部署指南.md`](deploy/飞书推送部署指南.md)**，以下是脚本本身的说明。

项目目录必须能被服务用户读取，数据库目录必须能被该用户写入。

```bash
# 以 root 运行（默认；部署指南采用这条）
sudo ./deploy/install-systemd.sh

# 或以专用用户运行
id winstock >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin winstock
sudo install -d -o winstock -g winstock data
# 注意：data/ 里已有的库文件及其 -wal/-shm 也必须一并 chown，否则服务写库失败
sudo chown winstock:winstock data/winstock.db data/winstock.db-wal data/winstock.db-shm 2>/dev/null
sudo env WINSTOCK_USER=winstock ./deploy/install-systemd.sh
```

安装脚本会在项目根目录创建 `.env`（`0600`）存放 Webhook。unit 文件是全局可读的，
所以密钥**不能**写进 unit 或 `ExecStart` 命令行（`ps` 同样可见）；`.env` 不在 unit
里，由程序自己读取。

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
