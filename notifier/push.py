"""消息推送模块 - Server酱 / PushPlus 通知"""
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


def send_notification(title: str, content: str, msg_type: str = "markdown") -> bool:
    """发送推送通知（自动选择可用通道）

    Args:
        title: 消息标题
        content: 消息内容（支持Markdown）
        msg_type: 消息类型

    Returns:
        是否发送成功
    """
    success = False

    # Server酱
    if SERVER_CHAN_KEY:
        success = _send_server_chan(title, content, msg_type) or success

    # PushPlus
    if PUSHPLUS_TOKEN:
        success = _send_pushplus(title, content, msg_type) or success

    if not success:
        logger.warning("未配置推送通道（SERVER_CHAN_KEY 或 PUSHPLUS_TOKEN），"
                       "请在 .env 中配置")

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
