"""信号服务 - 封装信号扫描、查询、推送逻辑与大盘风控仲裁"""
import logging
from typing import Optional, List, Dict

import pandas as pd

from engine.scanner import scan_signals
from data.storage import get_signals, save_signals_batch, get_index_daily, get_daily
from data.indicators import apply_indicators
from notifier.push import notify_signals, send_notification
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# 基于A股样本外实测校准的策略可信度先验权重（仅保留当前活跃注册策略）
STRATEGY_CREDIBILITY_WEIGHTS: Dict[str, float] = {
    "signal_combo": 1.08,
    "ma_cross": 1.05,
    "momentum_rotation": 1.05,
    "kdj_cross": 1.02,
    "bollinger_reversal": 0.92,
}


def classify_index_regime(
    close: float,
    ma20: float,
    prev_ma20: float,
    ma60: float,
    rsi14: float = 50.0,
) -> str:
    """全系统统一的大盘牛熊判定单一真相（回测引擎与实盘扫描共用）。"""
    if close > ma20 and ma20 >= prev_ma20 and rsi14 >= 50.0:
        return "BULL"
    if (close < ma60 and (close < ma20 or ma20 < ma60)) or (
        close < ma20 and ma20 < prev_ma20 and rsi14 < 42.0
    ):
        return "BEAR"
    return "RANGE"


def get_market_regime(end_date: str = "", index_code: str = "000300.SH") -> dict:
    """识别当前大盘或成长板块指数环境（多头进攻 / 震荡均衡 / 空头防守）并给出仓位与风格建议。"""
    target_code = str(index_code or "000300.SH").strip() or "000300.SH"
    default_regime = {
        "regime": "RANGE",
        "label": "🟡 震荡均衡",
        "max_position_pct": 50,
        "index_code": target_code,
        "close": 0.0,
        "ma20": 0.0,
        "rsi14": 50.0,
        "chg_5d_pct": 0.0,
        "advice": "大盘处于震荡区间，建议总仓位控制在 50% 以内，优选多策略共振与回踩企稳标的，严禁追高。",
    }
    try:
        fetch_limit = 2000 if end_date else 200
        df = get_index_daily(target_code, limit=fetch_limit)
        idx_code = target_code
        # 若默认的沪深300(000300.SH)最新交易日落后于上证指数，自动告警并回退到更及时的上证指数
        if target_code == "000300.SH":
            df_sh = get_index_daily("000001.SH", limit=fetch_limit)
            if df_sh is not None and len(df_sh) >= 25:
                sh_sub = df_sh[df_sh["trade_date"] <= end_date] if end_date else df_sh
                hs_sub = df[df["trade_date"] <= end_date] if (df is not None and not df.empty and end_date) else df
                sh_max = str(sh_sub["trade_date"].iloc[-1]) if not sh_sub.empty else ""
                hs_max = str(hs_sub["trade_date"].iloc[-1]) if (hs_sub is not None and not hs_sub.empty) else ""
                if sh_max and (not hs_max or sh_max > hs_max):
                    logger.warning(f"000300.SH 数据滞后({hs_max} < {sh_max})，大盘择时自动切换至 000001.SH")
                    df = df_sh
                    idx_code = "000001.SH"

        if df is None or len(df) < 25:
            df = get_index_daily("000001.SH", limit=fetch_limit)
            idx_code = "000001.SH"
        if (df is None or len(df) < 25) and target_code == "000300.SH":
            df = get_daily("510310.SH", end_date=end_date, limit=fetch_limit)
            idx_code = "510310.SH"
        if df is None or df.empty or len(df) < 20:
            return default_regime

        if end_date:
            df = df[df["trade_date"] <= end_date]
        if len(df) < 20:
            return default_regime

        df = apply_indicators(df.tail(220).copy(), ["ma", "rsi"])
        latest = df.iloc[-1]
        close = float(latest["close"])
        raw_m20 = latest.get("ma20", None)
        ma20 = float(raw_m20) if (raw_m20 is not None and pd.notna(raw_m20)) else close
        prev_m20_raw = df["ma20"].iloc[-3] if ("ma20" in df.columns and len(df) >= 3) else None
        prev_ma20 = float(prev_m20_raw) if (prev_m20_raw is not None and pd.notna(prev_m20_raw)) else ma20
        raw_m60 = latest.get("ma60", None)
        ma60 = float(raw_m60) if (raw_m60 is not None and pd.notna(raw_m60)) else ma20
        raw_rsi = latest.get("rsi14", None)
        rsi14 = float(raw_rsi) if (raw_rsi is not None and pd.notna(raw_rsi)) else 50.0
        close_5d_ago = float(df["close"].iloc[-6]) if len(df) >= 6 else float(df["close"].iloc[0])
        chg_5d = ((close / close_5d_ago) - 1.0) * 100.0 if close_5d_ago > 0 else 0.0

        regime = classify_index_regime(close, ma20, prev_ma20, ma60, rsi14)
        if regime == "BULL":
            label = "🟢 多头进攻"
            max_pos = 80
            advice = "指数站稳20日均线且趋势向上，适合顺势参与均线多头、放量突破及动量金叉信号（建议仓位 ≤ 80%）。"
        elif regime == "BEAR":
            label = "🔴 空头防守"
            max_pos = 25
            advice = "指数跌破中期生命线处于弱势防守期，假突破极多！已自动压降激进追高信号评分，建议总仓位 ≤ 25%，优先高股息防守或空仓观望。"
        else:
            label = "🟡 震荡均衡"
            max_pos = 50
            advice = "大盘处于多空拉锯震荡期，突破易冲高回落，建议总仓位 ≤ 50%，逢低关注均线回踩、红利价值与多策略共振信号。"

        return {
            "regime": regime,
            "label": label,
            "max_position_pct": max_pos,
            "index_code": idx_code,
            "close": round(close, 2),
            "ma20": round(ma20, 2),
            "rsi14": round(rsi14, 1),
            "chg_5d_pct": round(chg_5d, 2),
            "advice": advice,
        }
    except Exception as e:
        logger.warning(f"大盘环境识别异常: {e}")
        return default_regime


def get_dual_market_regime(end_date: str = "") -> dict:
    """双指数风格择时：大盘蓝筹看沪深300(000300.SH)，成长/高波看创业板指(399006.SZ)。

    防止「银行高股息拉盘护住沪深300、但成长中小盘普跌」的二八分化假安全假象。
    """
    large = get_market_regime(end_date=end_date, index_code="000300.SH")
    try:
        growth = get_market_regime(end_date=end_date, index_code="399006.SZ")
    except TypeError:
        growth = large

    large_allow = large.get("regime", "RANGE") != "BEAR"
    growth_allow = large_allow and (growth.get("regime", "RANGE") != "BEAR")

    if not large_allow:
        combined_regime = "BEAR"
    elif not growth_allow:
        combined_regime = "GROWTH_BEAR"
    else:
        combined_regime = large.get("regime", "RANGE")

    return {
        "large_cap": large,
        "growth": growth,
        "large_allow_buy": large_allow,
        "growth_allow_buy": growth_allow,
        "combined_regime": combined_regime,
        "summary": f"权重({large.get('index_code')}): {large.get('label')} | 成长(399006.SZ): {growth.get('label')}",
    }


class SignalService:
    """信号服务 - 封装信号扫描和查询的业务逻辑

    用法:
        svc = SignalService()
        signals = svc.scan(["000001.SZ"], ["ma_cross"], "20240101")
        history = svc.get_history(strategy="ma_cross", limit=50)
    """

    @staticmethod
    def scan(universe: list = None, strategy_names: list = None,
             end_date: str = "", save: bool = True,
             progress_callback=None, parallel: bool = True) -> list:
        """扫描信号

        Args:
            universe: 股票列表（None=全部有数据的股票）
            strategy_names: 策略名列表（None=全部策略）
            end_date: 扫描日期 YYYYMMDD
            save: 是否保存到数据库
            progress_callback: 进度回调
            parallel: 是否并行扫描

        Returns:
            Signal 列表，按 score 降序
        """
        return scan_signals(
            universe=universe,
            strategy_names=strategy_names,
            end_date=end_date,
            save=save,
            progress_callback=progress_callback,
            parallel=parallel,
        )

    @staticmethod
    def get_history(trade_date: str = "", strategy: str = "",
                    limit: int = 100):
        """查询历史信号"""
        return get_signals(trade_date=trade_date, strategy=strategy, limit=limit)

    @staticmethod
    def notify(signals: list, strategy_names: list = None):
        """推送信号通知"""
        if not signals:
            return False
        notify_signals(signals, strategy_names)
        return True

    @staticmethod
    def batch_save(signals: list):
        """批量保存信号"""
        if not signals:
            return 0
        save_signals_batch(signals)
        return len(signals)

    @staticmethod
    def arbitrate(signals: list, min_net_threshold: float = 0.15) -> list:
        """多策略同标的信号仲裁与多空净额结算"""
        return resolve_signal_conflicts(signals, min_net_threshold)


def resolve_signal_conflicts(signals: List, min_net_threshold: float = 0.15) -> list:
    """多策略同标的信号仲裁与多空净额结算

    功能:
        1. 多策略同向共振: 增强置信度得分，合并多策略触发原因。
        2. 多策略多空冲突: 计算多空净额 Net Score = sum(Buy) - sum(Sell)。
           分歧过大(差值 < min_net_threshold)予以对冲消除；明确偏向一方时输出净方向。
        3. 单策略独有信号: 保持原样输出。

    Args:
        signals: 原始 Signal 列表
        min_net_threshold: 多空博弈判定有效净额的最小差值阈值

    Returns:
        仲裁后的无冲突 Signal 列表，按 score 降序
    """
    from core.models import Signal
    if not signals:
        return []

    grouped = {}
    for s in signals:
        grouped.setdefault(s.ts_code, []).append(s)

    arbitrated = []
    for ts_code, sig_list in grouped.items():
        if len(sig_list) == 1:
            arbitrated.append(sig_list[0])
            continue

        trade_date = sig_list[0].trade_date
        price_ref = next((s.price_ref for s in sig_list if s.price_ref > 0), 0.0)

        buys = [s for s in sig_list if s.direction == "BUY"]
        sells = [s for s in sig_list if s.direction == "SELL"]

        # 1. 全部同向（共振增信）
        if buys and not sells:
            strategies = [s.strategy for s in buys]
            avg_score = sum(s.score for s in buys) / len(buys)
            boosted_score = min(1.0, avg_score + 0.08 * (len(buys) - 1))
            reasons = "; ".join(f"{s.strategy}: {s.reason}" for s in buys if s.reason)
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=f"ensemble_{len(buys)}",
                direction="BUY",
                score=round(boosted_score, 4),
                reason=f"[{len(buys)}策略多头共振: {', '.join(strategies)}] {reasons}",
                price_ref=price_ref,
                context_snapshot={
                    "is_ensemble": True,
                    "strategies": strategies,
                    "count": len(buys),
                }
            ))
        elif sells and not buys:
            strategies = [s.strategy for s in sells]
            avg_score = sum(s.score for s in sells) / len(sells)
            boosted_score = min(1.0, avg_score + 0.08 * (len(sells) - 1))
            reasons = "; ".join(f"{s.strategy}: {s.reason}" for s in sells if s.reason)
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=f"ensemble_{len(sells)}",
                direction="SELL",
                score=round(boosted_score, 4),
                reason=f"[{len(sells)}策略空头共振: {', '.join(strategies)}] {reasons}",
                price_ref=price_ref,
                context_snapshot={
                    "is_ensemble": True,
                    "strategies": strategies,
                    "count": len(sells),
                }
            ))
        else:
            # 2. 多空冲突对冲
            buy_score = sum(s.score for s in buys)
            sell_score = sum(s.score for s in sells)
            diff = buy_score - sell_score

            if abs(diff) < min_net_threshold:
                # 多空分歧极大，相互抵消，不产生交易
                continue

            if diff > 0:
                net_dir = "BUY"
                net_score = min(1.0, max(0.1, diff / len(sig_list) + 0.4))
                reason = f"[多空博弈净胜: {len(buys)}买vs{len(sells)}卖] 多头占优 (净分 {diff:+.2f})"
            else:
                net_dir = "SELL"
                net_score = min(1.0, max(0.1, abs(diff) / len(sig_list) + 0.4))
                reason = f"[多空博弈净胜: {len(sells)}卖vs{len(buys)}买] 空头占优 (净分 {diff:+.2f})"

            strategies = [f"{s.strategy}({s.direction})" for s in sig_list]
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy="arbitrated_net",
                direction=net_dir,
                score=round(net_score, 4),
                reason=reason,
                price_ref=price_ref,
                context_snapshot={
                    "is_arbitrated": True,
                    "strategies": strategies,
                    "buy_score": round(buy_score, 4),
                    "sell_score": round(sell_score, 4),
                }
            ))

    arbitrated.sort(key=lambda s: s.score, reverse=True)
    return arbitrated


def get_data_health_summary(max_valuation_age_days: int = 7) -> dict:
    """检查自选股行情、核心指数与估值快照的同步新鲜度，返回可读告警摘要。"""
    from datetime import datetime, timedelta
    from data.storage import (
        get_conn, get_watchlist, get_instrument_list, get_valuation_map,
    )

    out = {
        "has_issues": False,
        "wl_latest_date": "",
        "idx_latest_date": "",
        "lagging_stocks": [],
        "lagging_indices": [],
        "valuation_covered": 0,
        "valuation_total": 0,
        "valuation_missing_codes": [],
        "valuation_stale_count": 0,
        "summary_text": "✅ 数据与估值状态健康",
    }
    try:
        wl = get_watchlist()
        inst = get_instrument_list()
        name_map = dict(zip(inst["ts_code"], inst["name"])) if (inst is not None and not inst.empty) else {}
        wl_codes = [str(c).strip() for c in wl["ts_code"].tolist()] if (wl is not None and not wl.empty) else []

        with get_conn() as conn:
            # 1. 核心指数同步检查
            key_indices = ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ")
            ph_i = ",".join("?" * len(key_indices))
            idx_rows = conn.execute(
                f"SELECT ts_code, MAX(trade_date) FROM index_daily WHERE ts_code IN ({ph_i}) GROUP BY ts_code",
                list(key_indices),
            ).fetchall()
            idx_dates = {str(r[0]): str(r[1]) for r in idx_rows if r[1]}
            idx_ref = max(idx_dates.values()) if idx_dates else ""
            out["idx_latest_date"] = idx_ref
            if idx_ref:
                for ic in key_indices:
                    idt = idx_dates.get(ic, "")
                    if not idt or idt < idx_ref:
                        out["lagging_indices"].append({"ts_code": ic, "latest_date": idt or "缺失"})

            # 2. 自选股日线同步检查
            if wl_codes:
                ph_w = ",".join("?" * len(wl_codes))
                wl_rows = conn.execute(
                    f"SELECT ts_code, MAX(trade_date) FROM daily_price WHERE ts_code IN ({ph_w}) GROUP BY ts_code",
                    wl_codes,
                ).fetchall()
                wl_dates = {str(r[0]): str(r[1]) for r in wl_rows if r[1]}
                wl_ref = max(wl_dates.values()) if wl_dates else ""
                out["wl_latest_date"] = wl_ref
                if wl_ref:
                    for wc in wl_codes:
                        wdt = wl_dates.get(wc, "")
                        if not wdt or wdt < wl_ref:
                            out["lagging_stocks"].append({
                                "ts_code": wc,
                                "name": name_map.get(wc, wc),
                                "latest_date": wdt or "缺失",
                            })

        # 3. 非ETF自选股估值快照新鲜度
        non_etf_codes = [
            c for c in wl_codes
            if not c.upper().startswith(("51", "56", "58", "15", "16"))
        ]
        out["valuation_total"] = len(non_etf_codes)
        if non_etf_codes:
            val_map = get_valuation_map(non_etf_codes)
            cutoff = (datetime.now() - timedelta(days=int(max_valuation_age_days))).isoformat()
            missing = []
            stale = 0
            covered = 0
            for c in non_etf_codes:
                v = val_map.get(c)
                if not v:
                    missing.append(c)
                else:
                    covered += 1
                    upd = str(v.get("updated_at", "") or "")
                    if upd and upd < cutoff:
                        stale += 1
            out["valuation_covered"] = covered
            out["valuation_missing_codes"] = missing
            out["valuation_stale_count"] = stale

        parts = []
        if out["lagging_stocks"]:
            s_desc = "、".join(f"{x['name']}({x['ts_code']}停在{x['latest_date']})" for x in out["lagging_stocks"][:4])
            parts.append(f"行情滞后标的 {len(out['lagging_stocks'])} 只: {s_desc}")
        if out["lagging_indices"]:
            i_desc = "、".join(f"{x['ts_code']}({x['latest_date']})" for x in out["lagging_indices"])
            parts.append(f"滞后指数: {i_desc}")
        if out["valuation_total"] > 0 and (out["valuation_covered"] < out["valuation_total"] or out["valuation_stale_count"] > 0):
            parts.append(
                f"估值快照覆盖 {out['valuation_covered']}/{out['valuation_total']}"
                + (f"（过期 {out['valuation_stale_count']} 只）" if out["valuation_stale_count"] > 0 else "（缺失需补采）")
            )

        out["has_issues"] = bool(parts)
        if parts:
            out["summary_text"] = " | ".join(parts)
    except Exception as e:
        logger.debug(f"数据健康度检查异常: {e}")
    return out
