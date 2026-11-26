"""消息推送模块 - Server酱 / PushPlus / 企业微信 / 钉钉 / 飞书 通知"""
import json
import logging
import re
import ipaddress
from typing import List
from datetime import datetime
from urllib.parse import urlparse

import requests

from core.config import PROJECT_ROOT
from engine.commission import price_decimals, round_price, format_price

logger = logging.getLogger(__name__)


def _mask_url(url: str) -> str:
    """脱敏 URL（保留前 30 字符 + ***，防止 Webhook 密钥泄露到日志）"""
    if not url:
        return ""
    if len(url) <= 30:
        return url[:10] + "***"
    return url[:30] + "***"


_SECRET_RE = re.compile(
    r"(?i)((?:access_token|sendkey|secret|token|key|sign|password|webhook)[\"']?\s*[:=]\s*[\"']?)[^\s,}&\"']+"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _redact(text) -> str:
    """正则脱敏异常/响应体里的 URL 与密钥，保留其余诊断信息。

    旧实现只要异常消息含 "http" 就把整段替换成「请求异常（URL已脱敏）」，
    既丢光了诊断信息，又漏掉了不含 http 但含 token/secret 的错误。
    """
    s = str(text)
    s = _URL_RE.sub(lambda m: _mask_url(m.group(0)), s)
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", s)


def _safe_log(channel: str, error: Exception):
    """记录推送异常（URL / 密钥脱敏后保留可读的错误类型与消息）"""
    logger.error(f"{channel}推送异常: {type(error).__name__}: {_redact(error)}")


def _log_failed_response(channel: str, data):
    """只记录响应里的状态码与简短消息，绝不把整个响应体打进日志。

    官方接口的错误响应体有机会回显 token / Webhook 片段，
    旧实现 5 个通道都是 `logger.error(f"...: {data}")` 全量打印。
    """
    if isinstance(data, dict):
        brief = {k: _redact(data[k])[:120] for k in
                 ("errcode", "code", "errmsg", "msg", "message") if k in data}
    else:
        brief = _redact(data)[:120]
    logger.error(f"{channel}推送失败: {brief or '响应无状态字段'}")


# 官方 Webhook 域名白名单：防止 .env 被污染成内网地址后发起任意内网 POST
_WEBHOOK_ALLOWED_HOSTS = {
    "wecom": {"qyapi.weixin.qq.com"},
    "dingtalk": {"oapi.dingtalk.com"},
    "feishu": {"open.feishu.cn", "open.larksuite.com"},
}


def _validate_webhook_config(url: str, channel: str, allow_custom_host: bool = False) -> str:
    """加载时校验 Webhook 配置，不合法直接禁用该通道（返回空字符串）。

    校验项：仅 https、无 URL 内嵌凭据、host 命中官方白名单（可显式放宽）、
    host 为 IP 字面量时拒绝私网/环回/链路本地/保留地址（含云元数据 169.254.169.254）。
    """
    if not url:
        return ""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host or p.username or p.password:
            raise ValueError("必须使用 https 且不得在 URL 内嵌账号密码")
        if not allow_custom_host and host not in _WEBHOOK_ALLOWED_HOSTS.get(channel, set()):
            raise ValueError(f"域名 {host} 不在 {channel} 官方白名单内")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None                       # 普通域名，白名单已约束
        if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local
                               or ip.is_reserved or ip.is_multicast):
            raise ValueError("禁止使用私网/环回/元数据地址")
        return url
    except Exception as e:
        logger.warning(f"{channel} Webhook 配置不合法，已禁用该通道: {_mask_url(url)} ({e})")
        return ""


def _post_webhook(url: str, channel: str, payload: dict = None, form: dict = None):
    """统一 POST 出口：禁止自动跟随重定向（防止白名单域名 302 到内网）"""
    p = urlparse(url or "")
    if p.scheme != "https" or not p.netloc or p.username or p.password:
        logger.warning(f"{channel} Webhook URL 不合法，已拒绝请求: {_mask_url(url)}")
        return None
    try:
        ip = ipaddress.ip_address(p.hostname or "")
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            logger.warning(f"{channel} Webhook 指向内网地址，已拒绝请求: {_mask_url(url)}")
            return None
    except ValueError:
        pass  # 普通域名，加载时的白名单已校验
    kwargs = {"timeout": 10, "allow_redirects": False}
    if payload is not None:
        kwargs["json"] = payload
    if form is not None:
        kwargs["data"] = form
    return requests.post(url, **kwargs)


# 尝试从 .env 读取推送配置
import os
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

SERVER_CHAN_KEY = os.getenv("SERVER_CHAN_KEY", "").strip()
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "").strip()
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "").strip()    # 钉钉加签密钥（可选）

# 自建代理/网关场景可设 WEBHOOK_ALLOW_CUSTOM_HOST=true 放宽域名白名单，
# 但 https、无内嵌凭据、禁私网 IP、禁重定向这几条始终强制。
_ALLOW_CUSTOM_HOST = os.getenv("WEBHOOK_ALLOW_CUSTOM_HOST", "false").lower() == "true"
WECOM_WEBHOOK = _validate_webhook_config(os.getenv("WECOM_WEBHOOK", "").strip(), "wecom", _ALLOW_CUSTOM_HOST)
DINGTALK_WEBHOOK = _validate_webhook_config(os.getenv("DINGTALK_WEBHOOK", "").strip(), "dingtalk", _ALLOW_CUSTOM_HOST)
FEISHU_WEBHOOK = _validate_webhook_config(os.getenv("FEISHU_WEBHOOK", "").strip(), "feishu", _ALLOW_CUSTOM_HOST)


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
        resp = _post_webhook(url, "Server酱", form={
            "title": title,
            "desp": content,
        })
        if resp is None:
            return False
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"Server酱推送成功: {title}")
            return True
        else:
            _log_failed_response("Server酱", data)
            return False
    except Exception as e:
        _safe_log("Server酱", e)
        return False


def _send_pushplus(title: str, content: str, msg_type: str = "markdown") -> bool:
    """通过 PushPlus 推送（https://www.pushplus.plus）"""
    try:
        url = "https://www.pushplus.plus/send"
        resp = _post_webhook(url, "PushPlus", payload={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": msg_type,
        })
        if resp is None:
            return False
        data = resp.json()
        if data.get("code") == 200:
            logger.info(f"PushPlus推送成功: {title}")
            return True
        else:
            _log_failed_response("PushPlus", data)
            return False
    except Exception as e:
        _safe_log("PushPlus", e)
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
        # 企业微信 Markdown 消息格式，硬性限制单条不超过 4096 字节
        md_content = f"## {title}\n{content}"
        encoded = md_content.encode("utf-8")
        if len(encoded) > 4000:
            md_content = encoded[:3900].decode("utf-8", errors="ignore") + "\n\n*(内容过长已截断)*"
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": md_content},
        }
        resp = _post_webhook(WECOM_WEBHOOK, "企业微信", payload=payload)
        if resp is None:
            return False
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"企业微信推送成功: {title}")
            return True
        else:
            _log_failed_response("企业微信", data)
            return False
    except Exception as e:
        _safe_log("企业微信", e)
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
        md_text = f"## {title}\n\n{content}"
        encoded = md_text.encode("utf-8")
        if len(encoded) > 19000:
            md_text = encoded[:18500].decode("utf-8", errors="ignore") + "\n\n*(内容过长已截断)*"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": md_text,
            },
        }

        url = DINGTALK_WEBHOOK
        # 加签模式（安全设置→加签）
        if DINGTALK_SECRET:
            import time
            import hmac
            import hashlib
            import base64
            import urllib.parse
            timestamp = str(round(time.time() * 1000))
            sign_str = f"{timestamp}\n{DINGTALK_SECRET}"
            hmac_code = hmac.new(
                DINGTALK_SECRET.encode("utf-8"),
                sign_str.encode("utf-8"),
                hashlib.sha256,
            ).digest()
            signature = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            url = f"{url}&timestamp={timestamp}&sign={signature}"

        resp = _post_webhook(url, "钉钉", payload=payload)
        if resp is None:
            return False
        data = resp.json()
        if data.get("errcode") == 0:
            logger.info(f"钉钉推送成功: {title}")
            return True
        else:
            _log_failed_response("钉钉", data)
            return False
    except Exception as e:
        _safe_log("钉钉", e)
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
        resp = _post_webhook(FEISHU_WEBHOOK, "飞书", payload=payload)
        if resp is None:
            return False
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"飞书推送成功: {title}")
            return True
        else:
            _log_failed_response("飞书", data)
            return False
    except Exception as e:
        _safe_log("飞书", e)
        return False


def calc_atr_stops_and_sizing(ts_code: str, price: float, score: float = 0.7,
                              base_capital: float = 100000.0,
                              strategy: str = "",
                              position_scale: float = 1.0) -> dict:
    """基于标的最近14日真实波幅(ATR14)与策略风格预设，计算动态止损止盈位与风险平价建议股数"""
    price = max(float(price or 0.0), 0.001)
    scale = min(1.0, max(0.0, float(position_scale if position_scale is not None else 1.0)))
    atr_pct = 2.5  # 默认日均振幅 2.5% -> 2*ATR = 5% 止损
    try:
        from data.storage import get_daily
        from data.indicators import add_atr
        df = get_daily(ts_code, limit=25)
        if not df.empty and len(df) >= 14:
            df = add_atr(df, period=14)
            if "atr14" in df.columns and df["atr14"].notna().any():
                atr_val = float(df["atr14"].dropna().iloc[-1])
                latest_close = float(df["close"].iloc[-1]) if "close" in df.columns else price
                ref_p = latest_close if latest_close > 0 else price
                if atr_val > 0 and ref_p > 0:
                    atr_pct = (atr_val / ref_p) * 100.0
    except Exception:
        pass

    trail_activation = 0.0
    trail_callback = 0.0
    break_even_trigger = 0.0
    strat_key = str(strategy or "").strip()
    sl_pct = round(max(3.0, min(8.0, atr_pct * 2.0)), 1)
    tp_pct = round(max(6.0, min(16.0, sl_pct * 2.0)), 1)
    if strat_key:
        try:
            from engine.risk_manager import RiskManager
            if strat_key in RiskManager.STRATEGY_PRESETS:
                rm = RiskManager.for_strategy(strat_key, use_atr_stop=True)
                sl_pct = round(max(rm.atr_min_sl_pct, min(rm.atr_max_sl_pct, atr_pct * rm.atr_stop_mult)), 1)
                trail_activation = float(rm.trailing_stop_activation or 0.0)
                trail_callback = float(rm.trailing_stop_callback or 0.0)
                break_even_trigger = float(rm.break_even_trigger_pct) if rm.enable_break_even else 0.0
                tp_pct = round(max(sl_pct * 1.4, trail_activation if trail_activation > 0 else sl_pct * 2.0), 1)
        except Exception:
            pass

    stop_loss = round_price(price * (1.0 - sl_pct / 100.0), ts_code)
    take_profit = round_price(price * (1.0 + tp_pct / 100.0), ts_code)

    # 风险平价定仓：单笔最大承受总资金 1.2% ~ 1.8% 的止损风险（随信号评分与熊市缩放系数调整）
    risk_budget_pct = 0.010 + 0.008 * max(0.0, min(float(score or 0.7), 1.0))
    pos_pct = min(0.25, max(0.05, risk_budget_pct / (sl_pct / 100.0)))
    budget = base_capital * pos_pct * scale
    calc_shares = int(budget / price) // 100 * 100
    shares = calc_shares if calc_shares > 0 else (100 if (scale > 0 and base_capital >= price * 100) else 0)
    est_cost = round(shares * price, 2)

    return {
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "shares": shares,
        "est_cost": est_cost,
        "atr_pct": round(atr_pct, 2),
        "trail_activation": trail_activation,
        "trail_callback": trail_callback,
        "break_even_trigger": break_even_trigger,
        "strategy": strat_key,
        "position_scale": scale,
    }


_STRATEGY_CN_MAP = {
    "kdj_cross": "KDJ短线",
    "bollinger_reversal": "布林反转",
    "ma_cross": "双均线",
    "signal_combo": "多因子共振",
    "momentum_rotation": "动量轮动",
}


def _fmt_strategy_cn(name: str) -> str:
    return _STRATEGY_CN_MAP.get(name, name or "")


def notify_signals(signals, strategy_names: list = None, min_buy_score: float = None,
                   skip_held_buys: bool = False) -> bool:
    """推送信号摘要

    Args:
        signals: Signal对象列表
        strategy_names: 策略名称列表（用于标题）
        min_buy_score: 买入推荐最低评分门槛（None=读取 SCANNER_MIN_BUY_SCORE，默认 0.60）
        skip_held_buys: 是否跳过已持仓标的买入推荐（默认 False 保留持仓警告标注）

    Returns:
        是否推送成功
    """
    if not signals:
        return False

    try:
        from core.config import DEFAULT_CAPITAL, SCANNER_MIN_BUY_SCORE
        base_capital = float(DEFAULT_CAPITAL)
        min_score = float(SCANNER_MIN_BUY_SCORE) if min_buy_score is None else float(min_buy_score)
    except Exception:
        base_capital = 100000.0
        min_score = 0.60 if min_buy_score is None else float(min_buy_score)

    buy_count = sum(1 for s in signals if s.direction == "BUY")
    sell_count = sum(1 for s in signals if s.direction == "SELL")
    strategy_str = ", ".join(strategy_names) if strategy_names else "所有策略"
    today = datetime.now().strftime("%Y-%m-%d")

    # 延迟导入，避免循环依赖；查询标的中文名称
    from data.storage import get_instrument_name

    def _label(sig) -> str:
        """格式化标的名称为 '名称(代码)'"""
        name = get_instrument_name(sig.ts_code)
        return f"{name}({sig.ts_code})"

    title = f"📊 量化信号日报 - {today}"

    # 获取大盘与创业板双指数风控环境
    regime_line = ""
    combined_regime = "RANGE"
    try:
        from services.signal_service import get_dual_market_regime, get_market_regime
        try:
            dual = get_dual_market_regime()
            large_cap = dual.get("large_cap", {})
            growth = dual.get("growth", {})
            combined_regime = dual.get("combined_regime", large_cap.get("regime", "RANGE"))
            warn_suffix = " ｜ ⚠️ 创业板走熊分化·警惕高波成长追高" if combined_regime == "GROWTH_BEAR" else ""
            regime_line = (
                f"**大盘风控**: {large_cap.get('label', '🟡 震荡')}（建议总仓位 ≤ {large_cap.get('max_position_pct', 50)}%）"
                f" ｜ 创业板: {growth.get('label', '')}{warn_suffix}\n"
            )
        except Exception:
            regime_info = get_market_regime()
            combined_regime = regime_info.get("regime", "RANGE")
            regime_line = f"**大盘风控**: {regime_info['label']}（建议总仓位 ≤ {regime_info['max_position_pct']}%）\n"
    except Exception:
        pass

    # 结合真实持仓对买入信号标注组合护栏
    guard_meta = {}
    try:
        from engine.scanner import annotate_portfolio_guardrails
        signals, guard_meta = annotate_portfolio_guardrails(signals)
    except Exception:
        guard_meta = {}

    held_codes = set()
    try:
        from data.storage import get_positions
        held_codes = {
            str(p["ts_code"]).strip()
            for p in (get_positions() or [])
            if p.get("ts_code") and int(p.get("shares", 0) or 0) > 0
        }
    except Exception:
        held_codes = set()

    content = f"""## 📊 量化交易信号日报
**日期**: {today}
**策略**: {strategy_str}
{regime_line}**信号总数**: {len(signals)} 条（买入 {buy_count} | 卖出 {sell_count}）

---

### 🟢 买入信号 TOP5（量化行动指引）
"""

    # 冲突拦截与同股去重聚合：若同日某股有 SELL 信号且非买方压倒性占优，不进买入推荐；多策略同时买入同一标的时合并展示
    sell_by_code = {}
    for s in signals:
        if s.direction == "SELL":
            sell_by_code.setdefault(s.ts_code, []).append(s)

    buy_grouped = {}
    skipped_held_count = 0
    for s in signals:
        if s.direction == "BUY":
            if skip_held_buys and s.ts_code in held_codes:
                skipped_held_count += 1
                continue
            sells_for_code = sell_by_code.get(s.ts_code, [])
            if sells_for_code and max(x.score for x in sells_for_code) >= s.score - 0.05:
                continue
            buy_grouped.setdefault(s.ts_code, []).append(s)

    dedup_buys = []
    for code, b_list in buy_grouped.items():
        b_list.sort(key=lambda x: x.score, reverse=True)
        best = b_list[0]
        strat_cn = "+".join(dict.fromkeys(_fmt_strategy_cn(x.strategy) for x in b_list))
        if len(b_list) > 1 and "策略共振" not in (best.reason or ""):
            strat_names = "+".join(dict.fromkeys(x.strategy for x in b_list))
            merged_reason = f"[{len(b_list)}策略共振:{strat_names}] {best.reason}"
            from core.models import Signal as _Sig
            best = _Sig(
                ts_code=best.ts_code, trade_date=best.trade_date,
                strategy=best.strategy, direction="BUY",
                score=round(min(best.score + 0.05 * (len(b_list) - 1), 0.99), 2),
                reason=merged_reason, price_ref=best.price_ref,
            )
        dedup_buys.append((best, strat_cn))

    high_buys = [(s, sc) for s, sc in dedup_buys if s.score >= min_score]
    filtered_low_buy_count = len(dedup_buys) - len(high_buys)
    buy_signals = sorted(high_buys, key=lambda item: item[0].score, reverse=True)[:5]

    val_map = {}
    if buy_signals:
        try:
            from data.storage import get_valuation_map
            val_map = get_valuation_map([s.ts_code for s, _ in buy_signals]) or {}
        except Exception:
            val_map = {}

    if buy_signals:
        if guard_meta.get("at_capacity"):
            content += (
                f"> ⚠️ 当前实盘/模拟持仓已达上限（{guard_meta.get('position_count', 0)}/"
                f"{guard_meta.get('max_active_positions', 5)} 只），下列新买入信号仅作调仓替换参考。\n\n"
            )
        if skipped_held_count > 0:
            content += f"> 🛡️ 已自动跳过 {skipped_held_count} 条当前已持仓标的的重复买入推荐（防重复超买）。\n\n"
        content += (
            "| 股票 | 策略 | 评分 | 参考价 | 建议股数 | 预估金额 | 建议止损 | 目标止盈/风控 | 触发原因 |\n"
            "|------|------|------|--------|----------|----------|----------|----------|----------|\n"
        )
        for sig, strat_cn in buy_signals:
            price = max(float(sig.price_ref or 0.0), 0.001)
            is_cy_or_bear = (
                combined_regime == "BEAR"
                or (combined_regime == "GROWTH_BEAR" and str(sig.ts_code).startswith(("300", "301", "688")))
            )
            pos_scale = 0.5 if is_cy_or_bear else 1.0
            try:
                plan = calc_atr_stops_and_sizing(
                    sig.ts_code, price, sig.score, base_capital,
                    strategy=sig.strategy, position_scale=pos_scale,
                )
            except TypeError:
                plan = calc_atr_stops_and_sizing(sig.ts_code, price, sig.score, base_capital)
            if (
                pos_scale == 1.0
                and combined_regime == "GROWTH_BEAR"
                and float(plan.get("atr_pct", 0.0) or 0.0) >= 2.2
            ):
                pos_scale = 0.5
                try:
                    plan = calc_atr_stops_and_sizing(
                        sig.ts_code, price, sig.score, base_capital,
                        strategy=sig.strategy, position_scale=pos_scale,
                    )
                except TypeError:
                    pass
            shares = plan["shares"]
            est_cost = plan["est_cost"]
            stop_loss = plan["stop_loss"]
            take_profit = plan["take_profit"]
            sl_pct = plan["sl_pct"]
            tp_pct = plan["tp_pct"]
            trail_act = float(plan.get("trail_activation", 0.0) or 0.0)
            trail_cb = float(plan.get("trail_callback", 0.0) or 0.0)
            be_trig = float(plan.get("break_even_trigger", 0.0) or 0.0)
            rm_notes = []
            if trail_act > 0 and trail_cb > 0:
                rm_notes.append(f"回撤{trail_cb:g}%止盈")
            if be_trig > 0:
                rm_notes.append(f"浮盈{be_trig:g}%保本")
            rm_suffix = f" ({'/'.join(rm_notes)})" if rm_notes else ""
            scale_tag = "(熊市半仓)" if (pos_scale < 1.0 and shares > 0) else ""
            shares_text = f"{shares}股{scale_tag}" if shares > 0 else "资金不足1手"
            cost_text = f"¥{est_cost:,.0f}" if shares > 0 else "-"
            val_badge = ""
            if sig.ts_code in val_map:
                try:
                    from services.valuation_service import evaluate_valuation_guard
                    vg = evaluate_valuation_guard(sig.ts_code, val_map[sig.ts_code], ref_date=sig.trade_date)
                    val_badge = vg.get("badge") or ""
                except Exception:
                    val_badge = ""
            strat_col = f"{strat_cn} {val_badge}".strip() if val_badge else strat_cn
            safe_reason = (sig.reason or "").replace("|", "/")
            content += (
                f"| {_label(sig)} | {strat_col} | {sig.score:.2f} | {format_price(price, sig.ts_code)} | {shares_text} | {cost_text} "
                f"| {format_price(stop_loss, sig.ts_code)} (-{sl_pct:g}%) | {format_price(take_profit, sig.ts_code)} (+{tp_pct:g}%{rm_suffix}) | {safe_reason} |\n"
            )
    else:
        notes = []
        if filtered_low_buy_count > 0:
            notes.append(f"已自动拦截 {filtered_low_buy_count} 条低分弱信号")
        if skipped_held_count > 0:
            notes.append(f"已跳过 {skipped_held_count} 条已持仓买入")
        extra_msg = ("，" + "；".join(notes)) if notes else ""
        content += f"> 🛡️ 今日无满足高置信度门槛（≥{min_score:.2f}）的买入信号{extra_msg}，建议观望。\n"

    # 卖出区域按标的去重聚合，避免同股多策略卖出刷屏重复两行
    dedup_sells = []
    for code, s_list in sell_by_code.items():
        s_list.sort(key=lambda x: x.score, reverse=True)
        best_sell = s_list[0]
        strat_cn = "+".join(dict.fromkeys(_fmt_strategy_cn(x.strategy) for x in s_list))
        dedup_sells.append((best_sell, strat_cn))
    dedup_sells.sort(key=lambda item: item[0].score, reverse=True)

    content += "\n### 🔴 卖出信号 TOP5\n"
    if dedup_sells:
        content += (
            "| 股票 | 策略 | 评分 | 参考价 | 操作建议 | 触发原因 |\n"
            "|------|------|------|--------|----------|----------|\n"
        )
        for sig, strat_cn in dedup_sells[:5]:
            safe_reason = (sig.reason or "").replace("|", "/")
            content += f"| {_label(sig)} | {strat_cn} | {sig.score:.2f} | {format_price(sig.price_ref, sig.ts_code)} | 建议平仓/减半 | {safe_reason} |\n"
    else:
        content += "> 暂无卖出信号。\n"

    health_line = ""
    try:
        from services.signal_service import get_data_health_summary
        health = get_data_health_summary()
        if health.get("has_issues") and health.get("summary_text"):
            health_line = f"> 3. **⚠️ 数据健康提醒**：{health['summary_text']}。\n"
    except Exception:
        pass

    content += f"""
---
> 💡 **量化执行提示**：
> 1. 建议股数与金额基于基准资金 **¥{base_capital:,.0f}** 计算，可根据自身实际账户等比缩放。
> 2. 建议次日 9:25 集合竞价或开盘后分批择机成交；如遇跳空高开超 3% 不建议盲目追涨。
{health_line}"""

    return send_notification(title, content)


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


def notify_position_summary() -> bool:
    """推送持仓盈亏日报与实盘风控预警（ATR止损 / 移动止盈 / 保本平仓 / 时间止损）

    计算当前所有模拟持仓的浮动盈亏与风控处置建议，生成日报推送。
    数据来源：services.position_service.build_position_risk_report()。

    返回:
        True=已推送, False=跳过（无持仓或推送失败）
    """
    try:
        from data.storage import get_positions, get_daily, get_instrument_name, get_signals
        from services.position_service import build_position_risk_report

        positions = get_positions()
        if not positions:
            logger.info("持仓为空，跳过盈亏推送")
            return False

        report = build_position_risk_report(
            positions=positions,
            get_daily_fn=get_daily,
            get_signals_fn=get_signals,
            get_instrument_name_fn=get_instrument_name,
        )

        today = datetime.now().strftime("%Y-%m-%d")
        total_cost = report["total_cost"]
        total_market = report["total_market"]
        total_pnl = report["total_pnl"]
        total_pnl_pct = report["total_pnl_pct"]
        urgent_alerts = report["urgent_alerts"]
        profit_alerts = report["profit_alerts"]

        rows = []
        for item in report["items"]:
            ts_code = item["ts_code"]
            name = item["name"]
            buy_price = item["buy_price"]
            pnl = item["pnl"]
            pnl_pct = item["pnl_pct"]
            diag = item["diag"]
            current_price = diag["current_price"]
            icon = "🟢" if pnl >= 0 else "🔴"
            note_str = f" · {item['note']}" if item.get("note") else ""
            sl_px = diag["stop_loss_price"]
            max_g = diag["max_gain_pct"]
            trail_px = diag.get("trailing_stop_price")
            trail_txt = f" | 移动止盈{format_price(trail_px, ts_code)}" if trail_px is not None else ""
            rows.append(
                f"{icon} **{name}** ({ts_code}{note_str})  "
                f"成本{format_price(buy_price, ts_code)}→现{format_price(current_price, ts_code)}  "
                f"**{pnl:+.0f}元 ({pnl_pct:+.1f}%)**  "
                f"*(最高{max_g:+.1f}% | 止损{format_price(sl_px, ts_code)}{trail_txt} | {diag['action']})*"
            )

        total_icon = "🟢" if total_pnl >= 0 else "🔴"
        total_pnl_sign = "+" if total_pnl > 0 else "-" if total_pnl < 0 else ""

        alert_block = ""
        if urgent_alerts:
            alert_block += "### 🚨 持仓风控预警（需立即关注）\n" + "\n".join(f"- {a}" for a in urgent_alerts) + "\n\n"
        if profit_alerts:
            alert_block += "### 🎯 分批止盈提示\n" + "\n".join(f"- {a}" for a in profit_alerts) + "\n\n"

        alert_prefix = f"🚨 持仓风控预警({len(urgent_alerts)}项) · " if urgent_alerts else ""
        title = f"{alert_prefix}📊 持仓日报 - {today}"
        content = f"""## 📊 持仓风控日报
**日期**: {today}

{alert_block}**持仓汇总**
| 指标 | 数值 |
|:-----|:------|
| 持仓数 | {len(positions)} 只 (触发预警 {len(urgent_alerts)} 只) |
| 总成本 | ¥{total_cost:,.0f} |
| 总市值 | ¥{total_market:,.0f} |
| 总盈亏 | {total_icon} **{total_pnl_sign}¥{abs(total_pnl):,.0f} ({total_pnl_pct:+.1f}%)** |

**逐只明细与风控指令**
{chr(10).join(rows)}

---
*数据来源：A股量化交易系统 · 自动风控闭环推送*
"""
        send_notification(title, content)
        logger.info(
            f"持仓日报推送完成: 共 {len(positions)} 只持仓, "
            f"总盈亏 {total_pnl:+.0f}元, 风控预警 {len(urgent_alerts)} 项"
        )
        return True
    except Exception as e:
        logger.error(f"持仓日报推送失败: {type(e).__name__}: {e}")
        return False
