"""消息推送模块 - Server酱 / PushPlus / 企业微信 / 钉钉 通知"""
import json
import logging
from typing import List
from datetime import datetime

import requests

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


# 尝试从 .env 读取推送配置
import os
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")    # 企业微信机器人 Webhook URL
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")  # 钉钉机器人 Webhook URL
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "")     # 钉钉加签密钥（可选）
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")       # 飞书机器人 Webhook URL


def send_notification(title: str, content: str, msg_type: str = "markdown") -> bool:
    """发送推送通知（自动选择可用通道）

    Args:
        title: 消息标题
        content: 消息内容（支持Markdown）
        msg_type: 消息类型

    Returns:
        是否至少一个通道发送成功
    """
    success = False

    # Server酱
    if SERVER_CHAN_KEY:
        success = _send_server_chan(title, content, msg_type) or success

    # PushPlus
    if PUSHPLUS_TOKEN:
        success = _send_pushplus(title, content, msg_type) or success

    # 企业微信
    if WECOM_WEBHOOK:
        success = _send_wecom(title, content) or success

    # 钉钉
    if DINGTALK_WEBHOOK:
        success = _send_dingtalk(title, content) or success

    # 飞书
    if FEISHU_WEBHOOK:
        success = _send_feishu(title, content) or success

    if not success:
        logger.info("未配置推送通道（SERVER_CHAN_KEY / PUSHPLUS_TOKEN / "
                     "WECOM_WEBHOOK / DINGTALK_WEBHOOK / FEISHU_WEBHOOK），"
                     "请在 .env 中配置任一通道")

    return success


def _send_server_chan(title: str, content: str, msg_type: str = "markdown") -> bool:
    """通过 Server酱 推送（https://sct.ftqq.com）"""
    try:
        url = f"https://sctapi.ftqq.com/{SERVER_CHAN_KEY}.send"
        resp = requests.post(url, data={
            "title": title,
            "desp": content,
        }, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"Server酱推送成功: {title}")
            return True
        else:
            logger.error(f"Server酱推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"Server酱推送异常: {e}")
        return False


def _send_pushplus(title: str, content: str, msg_type: str = "markdown") -> bool:
    """通过 PushPlus 推送（https://www.pushplus.plus）"""
    try:
        url = "https://www.pushplus.plus/send"
        resp = requests.post(url, json={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": msg_type,
        }, timeout=10)
        data = resp.json()
        if data.get("code") == 200:
            logger.info(f"PushPlus推送成功: {title}")
            return True
        else:
            logger.error(f"PushPlus推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"PushPlus推送异常: {e}")
        return False


def _send_wecom(title: str, content: str) -> bool:
    """通过企业微信机器人推送

    配置: 在 .env 中设置 WECOM_WEBHOOK（群机器人 Webhook URL）

    Args:
        title: 消息标题
        content: Markdown 内容

    Returns:
        是否发送成功
    """
    try:
        # 企业微信 Markdown 消息格式
        md_content = f"## {title}\n{content}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": md_content},
        }
        resp = requests.post(WECOM_WEBHOOK, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"企业微信推送成功: {title}")
            return True
        else:
            logger.error(f"企业微信推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"企业微信推送异常: {e}")
        return False


def _send_dingtalk(title: str, content: str) -> bool:
    """通过钉钉机器人推送

    配置: 在 .env 中设置 DINGTALK_WEBHOOK（可选加签 DINGTALK_SECRET）

    Args:
        title: 消息标题
        content: Markdown 内容

    Returns:
        是否发送成功
    """
    try:
        # 钉钉 Markdown 消息格式
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"## {title}\n\n{content}",
            },
        }

        url = DINGTALK_WEBHOOK
        # 加签模式（安全设置→加签）
        if DINGTALK_SECRET:
            import time
            import hmac
            import hashlib
            import base64
            timestamp = str(round(time.time() * 1000))
            sign_str = f"{timestamp}\n{DINGTALK_SECRET}"
            signature = base64.b64encode(
                hmac.new(
                    DINGTALK_SECRET.encode("utf-8"),
                    sign_str.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
            ).decode("utf-8")
            url = f"{url}&timestamp={timestamp}&sign={signature}"

        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"钉钉推送成功: {title}")
            return True
        else:
            logger.error(f"钉钉推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"钉钉推送异常: {e}")
        return False


def _send_feishu(title: str, content: str) -> bool:
    """通过飞书机器人推送

    配置: 在 .env 中设置 FEISHU_WEBHOOK（群机器人 Webhook URL）

    飞书自定义机器人文档: https://open.feishu.cn/document/client-docs/bot-v2/add-custom-bot

    Args:
        title: 消息标题
        content: Markdown 内容

    Returns:
        是否发送成功
    """
    try:
        # 飞书 interactive 卡片消息，支持 Markdown
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content,
                    },
                    {
                        "tag": "hr",
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"A股量化交易系统 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                            }
                        ],
                    },
                ],
            },
        }
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"飞书推送成功: {title}")
            return True
        else:
            logger.error(f"飞书推送失败: {data}")
            return False
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")
        return False


def notify_signals(signals, strategy_names: list = None):
    """推送信号摘要

    Args:
        signals: Signal对象列表
        strategy_names: 策略名称列表（用于标题）
    """
    if not signals:
        return

    buy_count = sum(1 for s in signals if s.direction == "BUY")
    sell_count = sum(1 for s in signals if s.direction == "SELL")
    strategy_str = ", ".join(strategy_names) if strategy_names else "所有策略"
    today = datetime.now().strftime("%Y-%m-%d")

    title = f"📊 量化信号日报 - {today}"

    content = f"""## 📊 量化交易信号日报
**日期**: {today}
**策略**: {strategy_str}
**信号总数**: {len(signals)} 条（买入 {buy_count} | 卖出 {sell_count}）

---

### 🟢 买入信号 TOP5
| 股票 | 评分 | 参考价 | 原因 |
|------|------|--------|------|
"""

    buy_signals = sorted([s for s in signals if s.direction == "BUY"],
                         key=lambda s: s.score, reverse=True)[:5]
    for sig in buy_signals:
        content += f"| {sig.ts_code} | {sig.score:.2f} | ¥{sig.price_ref:.2f} | {sig.reason} |\n"

    content += """
### 🔴 卖出信号 TOP5
| 股票 | 评分 | 参考价 | 原因 |
|------|------|--------|------|
"""
    sell_signals = sorted([s for s in signals if s.direction == "SELL"],
                          key=lambda s: s.score, reverse=True)[:5]
    for sig in sell_signals:
        content += f"| {sig.ts_code} | {sig.score:.2f} | ¥{sig.price_ref:.2f} | {sig.reason} |\n"

    send_notification(title, content)


def notify_backtest_result(result):
    """推送回测结果"""
    if not result:
        return

    title = f"🔬 回测完成 - {result.strategy}"
    content = f"""## 回测结果 - {result.strategy}

| 指标 | 数值 |
|------|------|
| 总收益 | {result.total_return:.2f}% |
| 年化收益 | {result.annual_return:.2f}% |
| 最大回撤 | {result.max_drawdown:.2f}% |
| 夏普比率 | {result.sharpe_ratio:.2f} |
| 胜率 | {result.win_rate:.1f}% |
| 交易次数 | {result.trade_count} |
| 日期范围 | {result.start_date} ~ {result.end_date} |
"""
    send_notification(title, content)


def notify_position_summary():
    """推送持仓盈亏日报

    计算当前所有模拟持仓的浮动盈亏，生成日报推送。
    数据来源：storage.get_positions() + 最新收盘价。
    """
    try:
        from data.storage import get_positions, get_daily

        positions = get_positions()
        if not positions:
            logger.info("持仓为空，跳过盈亏推送")
            return

        today = datetime.now().strftime("%Y-%m-%d")
        total_cost = 0.0
        total_market = 0.0
        rows = []

        for pos in positions:
            ts_code = pos["ts_code"]
            buy_price = pos["buy_price"]
            shares = pos["shares"]
            cost = buy_price * shares

            # 取最新收盘价
            df = get_daily(ts_code, limit=1)
            if not df.empty:
                current_price = df.iloc[-1]["close"]
            else:
                current_price = buy_price  # 无数据则用成本价

            market_value = current_price * shares
            pnl = market_value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0

            total_cost += cost
            total_market += market_value

            icon = "🟢" if pnl >= 0 else "🔴"
            rows.append(
                f"{icon} **{ts_code}** ({pos.get('note', '')})  "
                f"成本¥{buy_price:.2f}→现¥{current_price:.2f}  "
                f"**{pnl:+.0f}元 ({pnl_pct:+.1f}%)**"
            )

        total_pnl = total_market - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        total_icon = "🟢" if total_pnl >= 0 else "🔴"

        title = f"📊 持仓日报 - {today}"
        content = f"""## 📊 持仓日报
**日期**: {today}

**持仓汇总**
| 指标 | 数值 |
|:-----|:------|
| 持仓数 | {len(positions)} 只 |
| 总成本 | ¥{total_cost:,.0f} |
| 总市值 | ¥{total_market:,.0f} |
| 总盈亏 | {total_icon} **¥{total_pnl:+,.0f} ({total_pnl_pct:+.1f}%)** |

**逐只明细**
{chr(10).join(rows)}

---
*数据来源：A股量化交易系统 · 自动推送*
"""
        send_notification(title, content)
        logger.info(f"持仓日报推送完成: 共 {len(positions)} 只持仓, "
                     f"总盈亏 {total_pnl:+.0f}元")
    except Exception as e:
        logger.error(f"持仓日报推送失败: {e}")
