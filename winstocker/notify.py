"""Feishu webhook delivery: pure card construction kept apart from the HTTP call."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .audit import DataAudit
from .candidates import Candidate
from .backtest import RotationResult
from .paper import PaperRebalance

LOG = logging.getLogger("winstocker")

FEISHU_TIMEOUT = 15
FEISHU_RETRIES = 3
# 飞书自定义机器人的请求体上限（官方 20 KB）。
FEISHU_MAX_BODY = 20 * 1024
# 失败原因上卡片前的截断长度。异常文本可能很长，不截断会撑爆请求体。
ERROR_MAX_CHARS = 500

WEBHOOK_ENV = "WINSTOCK_FEISHU_WEBHOOK"
SECRET_ENV = "WINSTOCK_FEISHU_SECRET"
# 配置写在项目根目录的 .env（该文件已在 .gitignore 中，本仓库是公开的）。
DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# 把飞书的错误码翻译成能直接照做的中文，避免用户面对一个裸数字。
FEISHU_ERROR_HINTS = {
    19021: "签名校验失败：核对 WINSTOCK_FEISHU_SECRET 是否与机器人后台一致，并检查服务器时钟",
    19022: "IP 不在白名单：机器人安全设置里的 IP 白名单未包含本机出口 IP",
    19024: "关键词不匹配：机器人设了自定义关键词，卡片正文必须包含该词",
    11232: "触发飞书限流，稍后重试即可",
    9499: "请求体格式错误",
}


@dataclass(frozen=True)
class NotifyTarget:
    webhook: str
    secret: str | None = None


@dataclass(frozen=True)
class PreviousPoolBacktest:
    snapshot_date: str
    report_path: str
    result: RotationResult


@dataclass(frozen=True)
class DailyDigest:
    """一天播报所需的全部素材。构造它不读数据库也不联网，因此可以直接测。"""
    generated_at: datetime
    data_date: str | None
    update_error: str | None
    data_error: str | None
    audit: DataAudit | None
    candidates: tuple[Candidate, ...]
    lookback: int
    min_history: int
    min_avg_amount: float
    snapshot_latest: str | None = None
    rows_added: int | None = None
    previous_pool_backtest: PreviousPoolBacktest | None = None
    backtest_error: str | None = None
    paper_updates: tuple[PaperRebalance, ...] = ()
    paper_error: str | None = None

    @property
    def status(self) -> str:
        """卡片颜色的唯一真相来源：更新/读库失败为红，审计有缺陷为橙，其余为绿。

        候选池为空不算失败——那是筛选门槛的正常结果，与 audit 对停牌和未上市的
        判断口径一致。
        """
        if self.data_error or self.update_error:
            return "error"
        if self.audit is None or not self.audit.ok:
            return "warn"
        return "ok"

    @property
    def header_template(self) -> str:
        return {"ok": "green", "warn": "orange", "error": "red"}[self.status]


def format_momentum(value: float) -> str:
    """0.1234 -> '+12.34%'。正号是刻意的：全为正时它是对齐锚点，出现负数一眼可见。"""
    return f"{value:+.2%}"


def format_amount(yuan: float) -> str:
    """按中文习惯分「亿」「万」两档。门槛是 2000 万，两档都会实际出现。"""
    if yuan >= 1e8:
        return f"{yuan / 1e8:.2f} 亿"
    return f"{yuan / 1e4:.0f} 万"


def feishu_signature(secret: str, timestamp: int) -> str:
    """飞书自定义机器人的签名。

    注意这里的参数顺序非常反直觉：拼接串是 HMAC 的 **key**，message 是**空的**。
    写成 hmac.new(secret, string_to_sign) 会稳定地验签失败（19021）。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def parse_dotenv(text: str) -> dict[str, str]:
    """解析 .env：支持 # 注释、空行、可选的 export 前缀、值两侧成对的引号。

    刻意不做变量展开和多行值——这个文件只有两个键，多一分解析复杂度就多一类
    「为什么没生效」。
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def dotenv_values(path: Path = DOTENV_PATH) -> dict[str, str]:
    """读 .env。文件不存在或不可读都返回空字典——配置缺失由调用方给出提示。"""
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def credential(flag_value: str | None, env_name: str, environ: Mapping[str, str] = os.environ,
               dotenv: Mapping[str, str] | None = None) -> str | None:
    """按 命令行参数 > 环境变量 > .env 取配置；空字符串一律视为未配置。

    真实环境变量优先于 .env，与 python-dotenv 的约定一致，也与 systemd 单元被
    临时覆盖时的直觉一致。
    """
    if flag_value and flag_value.strip():
        return flag_value.strip()
    value = environ.get(env_name, "").strip()
    if value:
        return value
    source = dotenv_values() if dotenv is None else dotenv
    return source.get(env_name, "").strip() or None


def require_credential(flag_value: str | None, env_name: str, label: str,
                       environ: Mapping[str, str] = os.environ,
                       dotenv: Mapping[str, str] | None = None) -> str:
    value = credential(flag_value, env_name, environ, dotenv)
    if value is None:
        raise RuntimeError(
            f"未配置{label}。请在 {DOTENV_PATH} 里写一行 {env_name}=...，"
            f"或临时用命令行参数指定（参见 deploy/飞书推送部署指南.md）"
        )
    return value


def _health_block(digest: DailyDigest) -> str:
    """数据健康段落。刻意不出现 audit 的字段名——看卡片的人不需要知道什么叫 integrity_check。"""
    audit = digest.audit
    if audit is None:
        return "**数据健康：不可用**\n无法读取本地数据库，详见下方原因。"
    verdict = "通过" if audit.ok else "**需检查**"
    lines = [f"**数据健康：{verdict}**"]
    if digest.data_date:
        lines.append(f"完整覆盖截至 **{digest.data_date}**（{audit.latest_day_symbols} 只）")
    if digest.rows_added is not None:
        lines.append(f"本次新增日 K：**{digest.rows_added:,}** 行")
    lines.append(f"下载失败待重试：**{audit.failures}**　落后且非停牌：**{audit.lagging_failed}**")
    if audit.broken_change_rows:
        lines.append(
            f"历史价格序列自洽性：**异常**（{audit.broken_change_rows} 行尺度漂移，"
            f"回测收益率不可信，下次 update 会自动重抓修复）"
        )
    else:
        lines.append("历史价格序列自洽性：正常")
    return "\n".join(lines)


def _failure_block(digest: DailyDigest) -> str | None:
    if digest.update_error:
        return ("🚨 **今天的数据没有更新成功**\n"
                f"失败原因：{digest.update_error}\n"
                "下面的候选池基于库中已有的数据，仅供参考，请勿据此操作。")
    if digest.data_error:
        return f"🚨 **无法读取本地数据库**\n原因：{digest.data_error}"
    return None


def _candidate_block(digest: DailyDigest) -> str:
    scope = (f"{digest.lookback} 日动量 · {digest.min_history} 日历史 · "
             f"{digest.lookback} 日均额 ≥ {format_amount(digest.min_avg_amount)}")
    if not digest.candidates:
        # 必须明说「这是正常的」，否则非技术用户看到空列表第一反应是系统坏了。
        return (f"**候选池：今日无股票通过筛选门槛**\n"
                f"这是筛选条件的正常结果，不代表数据有问题。当前门槛：{scope}。")
    lines = [f"**候选池 Top {len(digest.candidates)}**（{scope}）", ""]
    for number, item in enumerate(digest.candidates, start=1):
        lines.append(
            f"{number}. **{item.symbol} {item.name}** · 动量 **{format_momentum(item.momentum)}**"
            f" · 均额 {format_amount(item.average_amount)}"
        )
    return "\n".join(lines)


def _backtest_block(digest: DailyDigest) -> str:
    item = digest.previous_pool_backtest
    if item is None:
        reason = digest.backtest_error or "尚无早于本次数据日的候选快照"
        return f"**上一日候选池回测：暂不可用**\n{reason}。"
    result = item.result
    annualized = "-" if result.annualized_return is None else f"{result.annualized_return:.2%}"
    return (
        f"**上一日候选池回测**（快照 {item.snapshot_date}，{len(result.symbols)} 只）\n"
        f"20 日动量前 3 · 每 20 日调仓 · 区间 {result.start} 至 {result.end}\n"
        f"总收益 **{result.total_return:+.2%}** · 年化 {annualized} · 最大回撤 "
        f"**{result.max_drawdown:.2%}** · 成交 {result.trades} 笔\n"
        f"涨停未买 {result.blocked_buys} · 跌停未卖 {result.blocked_sells} · "
        f"持仓停牌 {result.suspension_days} 天\n"
        "这是按上一日名单做的回顾性诊断，不是样本外收益证明。"
    )


def _paper_block(digest: DailyDigest) -> str:
    if digest.paper_error:
        return f"**自动模拟交易：异常**\n{digest.paper_error}"
    if not digest.paper_updates:
        return "**自动模拟交易：未执行**\n没有启用的模拟策略账户，或本次为只读预览。"
    sections: list[str] = []
    for update in digest.paper_updates:
        status = update.status
        pnl = status.total_value / status.initial_cash - 1
        lines = [
            f"**模拟账户 {update.account}** · {update.trade_date}",
            update.note,
            f"总权益 **{status.total_value:,.2f}** · 累计收益 **{pnl:+.2%}** · "
            f"现金 {status.cash:,.2f} · 持仓 {status.positions} 只",
        ]
        for fill in update.fills:
            side = "买入" if fill.side == "BUY" else "卖出"
            lines.append(
                f"- {side} **{fill.symbol}** {fill.shares} 股 @ {fill.price:.3f}"
                f"（费税 {fill.fee + fill.tax:.2f}）"
            )
        if not update.fills:
            lines.append("- 今日无成交")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def build_card(digest: DailyDigest) -> dict[str, Any]:
    """构造 interactive 卡片（v1 结构：elements 在顶层）。

    刻意不使用 card JSON 2.0：v2 要求飞书客户端 7.20+，低版本只显示标题加一句升级
    提示；v2 还会对未知属性直接报错，而 v1 静默忽略。这里的内容不需要 v2 才有的
    能力，稳定性优先。日期用**数据日期**而非今天——休市日收到写着前一交易日的
    标题，用户自然就懂了，不需要额外解释。
    """
    elements: list[dict[str, Any]] = []
    failure = _failure_block(digest)
    if failure:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": failure}})
        elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _health_block(digest)}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _candidate_block(digest)}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _backtest_block(digest)}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _paper_block(digest)}})
    elements.append({"tag": "hr"})
    footer = f"生成于 {digest.generated_at:%Y-%m-%d %H:%M:%S}"
    if digest.snapshot_latest:
        footer += f" · 已固化快照 {digest.snapshot_latest}"
    footer += " · 仅供研究，不构成投资建议，不产生任何交易指令。"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"WinStock 每日播报 · {digest.data_date or '数据不可用'}"},
            "template": digest.header_template,
        },
        "elements": elements,
    }


def build_test_card(generated_at: datetime, secret_configured: bool) -> dict[str, Any]:
    """配置自检卡片。刻意不读数据库：这样即使库坏了也能单独验证飞书链路。"""
    content = "\n".join([
        "✅ **飞书推送配置成功**",
        f"服务器时间：{generated_at:%Y-%m-%d %H:%M:%S}（北京时间）",
        f"签名校验：{'已开启' if secret_configured else '未开启'}",
        "",
        "看到这张卡片说明 Webhook、网络和卡片渲染都正常。",
        "真正每日播报会在数据更新后自动发送，内容含候选池与数据健康状态。",
    ])
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "WinStock 配置自检"},
                   "template": "blue"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
    }


def build_payload(card: dict[str, Any], secret: str | None = None,
                  timestamp: int | None = None) -> dict[str, Any]:
    """组装最终请求体。未配置密钥时不带 sign 字段，而不是带一个空的 sign。"""
    payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
    if secret:
        moment = timestamp if timestamp is not None else int(time.time())
        payload["timestamp"] = str(moment)
        payload["sign"] = feishu_signature(secret, moment)
    return payload


def post_json(url: str, payload: dict[str, Any], retries: int = FEISHU_RETRIES,
              timeout: int = FEISHU_TIMEOUT) -> dict[str, Any]:
    """POST JSON with bounded retries.

    只对网络层异常与 HTTP 5xx 重试。飞书的应用级错误（19021 签名错、19024 关键词
    不匹配）是确定性的，重试既无意义，又会白白加速触发限流。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST",
                      headers={"Content-Type": "application/json; charset=utf-8"})
    last_error: RuntimeError | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            last_error = RuntimeError(f"飞书返回 HTTP {error.code}")
            if error.code < 500:
                break  # 4xx 重试不会变好
        except Exception as error:
            # 绝不把异常整个 repr 出去：URLError 带着 .url，而 webhook 地址本身就是凭据。
            last_error = RuntimeError(f"{type(error).__name__}: {getattr(error, 'reason', error)}")
        if attempt < retries - 1:
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"飞书请求失败: {last_error}") from last_error


def send_card(target: NotifyTarget, card: dict[str, Any], timestamp: int | None = None) -> None:
    """发送卡片。成功返回 None，失败抛 RuntimeError（消息中不含 webhook 地址）。"""
    response = post_json(target.webhook, build_payload(card, target.secret, timestamp))
    code = response.get("code")
    if code != 0:
        hint = FEISHU_ERROR_HINTS.get(code)
        detail = f"｜{hint}" if hint else ""
        raise RuntimeError(f"飞书拒绝接收：code={code} msg={response.get('msg')}{detail}")
    LOG.info("飞书卡片已送达。")
