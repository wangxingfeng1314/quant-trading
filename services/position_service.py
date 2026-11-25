"""实盘持仓风控诊断与预警服务 (Position Risk Diagnosis Service)

补齐「回测靠 RiskManager 移动止盈/保本/ATR止损赚钱、实盘推送却缺失持仓风控」的关键断层。
"""
import logging
from datetime import datetime
from typing import Optional

from data.storage import get_daily, get_signals, get_positions, get_instrument_name, get_watchlist
from services.strategy_recommender import parse_recommended_strategy_key

logger = logging.getLogger(__name__)

_STRATEGY_CN_FALLBACK = {
    "kdj_cross": "KDJ金叉死叉",
    "bollinger_reversal": "布林带触轨反转",
    "ma_cross": "双均线交叉",
    "signal_combo": "多因子共振",
    "momentum_rotation": "多资产动量轮动",
}


def _format_strategy_cn_safe(name: str) -> str:
    if not name:
        return ""
    if name in _STRATEGY_CN_FALLBACK:
        return _STRATEGY_CN_FALLBACK[name]
    try:
        from app.st_utils import format_strategy_cn
        return format_strategy_cn(name)
    except Exception:
        return str(name)


def _parse_yyyymmdd(val: str) -> Optional[datetime]:
    clean = str(val or "").replace("-", "").replace("/", "").strip()[:8]
    if len(clean) != 8 or not clean.isdigit():
        return None
    try:
        return datetime.strptime(clean, "%Y%m%d")
    except ValueError:
        return None


def _resolve_strategy_hint(ts_code: str, strategy_hint: str = "") -> str:
    raw = str(strategy_hint or "").strip()
    if raw:
        parsed = parse_recommended_strategy_key(raw)
        return parsed or raw
    try:
        wl = get_watchlist()
        if wl is not None and not wl.empty and "recommended_strategy" in wl.columns:
            match = wl[wl["ts_code"] == ts_code]
            if not match.empty:
                return parse_recommended_strategy_key(match.iloc[0].get("recommended_strategy", ""))
    except Exception:
        pass
    return ""


def diagnose_position_action(
    ts_code: str,
    buy_price: float,
    buy_date: str = "",
    strategy_hint: str = "",
    get_daily_fn=None,
    get_signals_fn=None,
    calc_atr_fn=None,
) -> dict:
    """诊断单只持仓标的的实时风控状态与止损/保本/移动止盈/时间止损操作指令。"""
    daily_fn = get_daily_fn or get_daily
    signals_fn = get_signals_fn or get_signals
    if calc_atr_fn is None:
        from notifier.push import calc_atr_stops_and_sizing
        atr_fn = calc_atr_stops_and_sizing
    else:
        atr_fn = calc_atr_fn

    buy_px = max(float(buy_price or 0.0), 0.01)
    resolved_hint = _resolve_strategy_hint(ts_code, strategy_hint)
    is_reversal = resolved_hint in ("kdj_cross", "bollinger_reversal")

    plan = atr_fn(ts_code, buy_px, score=0.7)
    atr_pct = float(plan.get("atr_pct") or 2.5)

    if is_reversal:
        sl_pct = round(max(4.5, min(10.0, atr_pct * 3.0)), 1)
        tp_pct = round(max(6.0, min(14.0, sl_pct * 1.4)), 1)
        max_hold_limit = 60
        max_hold_pnl_cap = 2.0
        enable_be = False
        be_trigger = 999.0
        be_buffer = 0.0
        trail_act = 10.0
        trail_cb = 3.5
    else:
        sl_pct = float(plan.get("sl_pct", 5.0))
        tp_pct = float(plan.get("tp_pct", 10.0))
        max_hold_limit = 0
        max_hold_pnl_cap = None
        enable_be = True
        be_trigger = 5.0
        be_buffer = 0.5
        trail_act = 10.0
        trail_cb = 3.5

    stop_loss_price = round(buy_px * (1.0 - sl_pct / 100.0), 2)
    take_profit_price = round(buy_px * (1.0 + tp_pct / 100.0), 2)

    df = daily_fn(ts_code, limit=60)
    current_price = float(df.iloc[-1]["close"]) if (df is not None and not df.empty) else buy_px
    latest_trade_date = str(df.iloc[-1]["trade_date"]) if (df is not None and not df.empty and "trade_date" in df.columns) else ""
    pnl_pct = (current_price / buy_px - 1.0) * 100.0

    # 持仓天数计算
    holding_days = 0
    b_dt = _parse_yyyymmdd(buy_date)
    ref_dt = _parse_yyyymmdd(latest_trade_date) or datetime.now()
    if b_dt is not None:
        holding_days = max(0, (ref_dt - b_dt).days)

    # 建仓以来最高价与高点回撤
    peak_price = max(buy_px, current_price)
    if df is not None and not df.empty:
        clean_buy = str(buy_date or "").replace("-", "").replace("/", "").strip()[:8]
        sub_df = df[df["trade_date"].astype(str) >= clean_buy] if (clean_buy and "trade_date" in df.columns) else df.tail(10)
        if not sub_df.empty and "high" in sub_df.columns:
            max_hi = float(sub_df["high"].max())
            if max_hi == max_hi and max_hi > 0:
                peak_price = max(peak_price, max_hi)

    max_gain_pct = (peak_price / buy_px - 1.0) * 100.0
    drawdown_from_peak = ((peak_price - current_price) / peak_price * 100.0) if peak_price > 0 else 0.0
    trailing_stop_price = round(peak_price * (1.0 - trail_cb / 100.0), 2) if max_gain_pct >= trail_act else None

    # 查询近期策略 SELL 信号
    recent_sell_str = ""
    try:
        sig_df = signals_fn(ts_code=ts_code, direction="SELL", limit=3)
        if sig_df is not None and not sig_df.empty:
            top_sell = sig_df.iloc[0]
            sig_date = str(top_sell.get("trade_date", ""))
            sig_score = float(top_sell.get("score", 0.0) or 0.0)
            is_recent = True
            s_dt = _parse_yyyymmdd(sig_date)
            l_dt = _parse_yyyymmdd(latest_trade_date)
            if s_dt is not None and l_dt is not None:
                is_recent = abs((l_dt - s_dt).days) <= 5
            elif latest_trade_date:
                is_recent = sig_date >= latest_trade_date[:6]
            if sig_score >= 0.55 and is_recent:
                recent_sell_str = _format_strategy_cn_safe(str(top_sell.get("strategy", "")))
    except Exception:
        pass

    # 严格按风控优先级生成操作指令
    if pnl_pct <= -sl_pct:
        action = f"🚨 跌破ATR止损(-{sl_pct:.1f}%)·建议止损清仓"
        level = "danger"
    elif max_gain_pct >= trail_act and drawdown_from_peak >= trail_cb and pnl_pct > 1.0:
        action = f"⚠️ 高点回撤({drawdown_from_peak:.1f}%，触发¥{trailing_stop_price:.2f})·建议移动止盈锁利"
        level = "warning"
    elif enable_be and max_gain_pct >= be_trigger and pnl_pct <= be_buffer:
        action = f"🛡️ 回撤至保本线(最高+{max_gain_pct:.1f}%→现{pnl_pct:+.1f}%)·建议保本平仓"
        level = "warning"
    elif recent_sell_str:
        action = f"⚠️ 触发卖出信号({recent_sell_str})·建议减仓/离场"
        level = "warning"
    elif max_hold_limit > 0 and holding_days >= max_hold_limit and (max_hold_pnl_cap is None or pnl_pct < max_hold_pnl_cap):
        action = f"⏱️ 持仓已达{holding_days}天仍无起色(超{max_hold_limit}天且盈亏{pnl_pct:+.1f}%)·建议时间止损调仓"
        level = "warning"
    elif pnl_pct >= tp_pct:
        action = f"🎯 达到目标止盈(+{tp_pct:.1f}%)·可分批止盈"
        level = "profit"
    else:
        trail_suffix = f" / 移动止盈¥{trailing_stop_price:.2f}" if trailing_stop_price is not None else ""
        action = f"✅ 趋势正常·持有(止损¥{stop_loss_price:.2f}{trail_suffix})"
        level = "hold"

    return {
        "current_price": current_price,
        "pnl_pct": round(pnl_pct, 2),
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "peak_price": round(peak_price, 2),
        "max_gain_pct": round(max_gain_pct, 2),
        "drawdown_from_peak": round(drawdown_from_peak, 2),
        "holding_days": holding_days,
        "trailing_stop_price": trailing_stop_price,
        "strategy_hint": resolved_hint,
        "action": action,
        "level": level,
    }


def build_position_risk_report(
    positions: Optional[list] = None,
    get_positions_fn=None,
    get_daily_fn=None,
    get_signals_fn=None,
    get_instrument_name_fn=None,
) -> dict:
    """汇总全部模拟持仓的风控诊断报告，供推送日报与持仓雷达页共用。"""
    pos_fn = get_positions_fn or get_positions
    name_fn = get_instrument_name_fn or get_instrument_name
    pos_list = pos_fn() if positions is None else positions

    if not pos_list:
        return {
            "positions": [],
            "items": [],
            "total_cost": 0.0,
            "total_market": 0.0,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "urgent_alerts": [],
            "profit_alerts": [],
        }

    total_cost = 0.0
    total_market = 0.0
    items = []
    urgent_alerts = []
    profit_alerts = []

    for pos in pos_list:
        ts_code = pos["ts_code"]
        buy_price = float(pos.get("buy_price", 0.0) or 0.0)
        shares = int(pos.get("shares", 0) or 0)
        buy_date = str(pos.get("buy_date") or "")

        diag = diagnose_position_action(
            ts_code=ts_code,
            buy_price=buy_price,
            buy_date=buy_date,
            strategy_hint=str(pos.get("strategy") or "").strip(),
            get_daily_fn=get_daily_fn,
            get_signals_fn=get_signals_fn,
        )
        curr_px = diag["current_price"]
        cost = buy_price * shares
        market_value = curr_px * shares
        pnl = market_value - cost
        pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0

        total_cost += cost
        total_market += market_value

        name = name_fn(ts_code).replace("*", "\\*")
        entry = {
            "ts_code": ts_code,
            "name": name,
            "note": str(pos.get("note") or "").strip(),
            "buy_price": buy_price,
            "shares": shares,
            "buy_date": buy_date,
            "cost": cost,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "diag": diag,
        }
        items.append(entry)

        alert_line = (
            f"**{name}** ({ts_code}) 现¥{curr_px:.2f}（{pnl_pct:+.1f}%）→ {diag['action']}"
        )
        if diag["level"] in ("danger", "warning"):
            urgent_alerts.append(alert_line)
        elif diag["level"] == "profit":
            profit_alerts.append(alert_line)

    total_pnl = total_market - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
    return {
        "positions": pos_list,
        "items": items,
        "total_cost": total_cost,
        "total_market": total_market,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "urgent_alerts": urgent_alerts,
        "profit_alerts": profit_alerts,
    }


def summarize_trade_journal(journal_df=None) -> dict:
    """汇总实盘账本已实现盈亏、胜率、滑点损耗及按策略归因统计。"""
    import pandas as pd
    if journal_df is None:
        from data.storage import get_trade_journal
        journal_df = get_trade_journal(limit=1000)

    if journal_df is None or journal_df.empty:
        return {
            "closed_count": 0,
            "buy_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl_pct": 0.0,
            "avg_holding_days": 0.0,
            "avg_slippage_pct": 0.0,
            "by_strategy": [],
        }

    buy_df = journal_df[journal_df["direction"] == "BUY"]
    sell_df = journal_df[journal_df["direction"] == "SELL"]
    slip_valid = journal_df[journal_df["signal_price"].fillna(0) > 0]
    avg_slip = float(slip_valid["slippage_pct"].mean()) if not slip_valid.empty else 0.0

    if sell_df.empty:
        return {
            "closed_count": 0,
            "buy_count": int(len(buy_df)),
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl_pct": 0.0,
            "avg_holding_days": 0.0,
            "avg_slippage_pct": round(avg_slip, 3),
            "by_strategy": [],
        }

    wins = sell_df[sell_df["pnl"] > 0]
    losses = sell_df[sell_df["pnl"] <= 0]
    closed_cnt = int(len(sell_df))

    by_strategy = []
    sell_work = sell_df.copy()
    sell_work["strat_key"] = sell_work["strategy"].fillna("").replace("", "手动/未标注")
    for s_key, grp in sell_work.groupby("strat_key"):
        w_cnt = int((grp["pnl"] > 0).sum())
        n_cnt = int(len(grp))
        by_strategy.append({
            "策略": _format_strategy_cn_safe(str(s_key)),
            "平仓笔数": n_cnt,
            "胜率%": round(w_cnt / n_cnt * 100.0, 1) if n_cnt > 0 else 0.0,
            "累计盈亏(元)": round(float(grp["pnl"].sum()), 2),
            "平均盈亏%": round(float(grp["pnl_pct"].mean()), 2),
            "平均持有天": round(float(grp["holding_days"].mean()), 1),
        })
    by_strategy.sort(key=lambda r: r["累计盈亏(元)"], reverse=True)

    return {
        "closed_count": closed_cnt,
        "buy_count": int(len(buy_df)),
        "win_count": int(len(wins)),
        "loss_count": int(len(losses)),
        "win_rate": round(len(wins) / closed_cnt * 100.0, 1),
        "total_pnl": round(float(sell_df["pnl"].sum()), 2),
        "avg_pnl_pct": round(float(sell_df["pnl_pct"].mean()), 2),
        "avg_holding_days": round(float(sell_df["holding_days"].mean()), 1),
        "avg_slippage_pct": round(avg_slip, 3),
        "by_strategy": by_strategy,
    }
