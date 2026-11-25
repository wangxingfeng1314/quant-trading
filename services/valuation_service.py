"""估值过滤与核心-卫星仓位配置服务 (Valuation & Core-Satellite Service)

职责:
  1. 从 Tushare / AKShare 尽力抓取个股估值快照（PE-TTM/PB/股息率/总市值）并缓存到 SQLite；
  2. 为买入信号提供轻量估值与财报窗口守卫（拦截亏损股/极端高估值盲目左侧抄底，鼓励低估高息）；
  3. 基于滚动 60 日 ATR% 与标的类型，生成「核心底仓 70% + 卫星趋势 30%」实盘仓位规划方案。
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

from data.storage import (
    get_daily, get_instrument_list, get_valuation_map, save_valuation_batch,
)
from data.indicators import add_atr

logger = logging.getLogger(__name__)


def _to_float(val) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _is_etf_code(ts_code: str) -> bool:
    code = str(ts_code or "").strip().upper()
    return code.startswith(("51", "56", "58", "15", "16"))


def _to_baostock_code(ts_code: str) -> str:
    code = str(ts_code or "").strip().upper()
    if "." not in code:
        return ""
    sym, exch = code.split(".", 1)
    if exch == "SH":
        return f"sh.{sym}"
    if exch == "SZ":
        return f"sz.{sym}"
    return ""


def _fetch_valuations_baostock(stock_codes: list[str], now_iso: str) -> dict[str, dict]:
    """通过 Baostock 单股日频指标通道拉取 PE-TTM、PB-MRQ 及 3 年历史 PE 分位。"""
    out: dict[str, dict] = {}
    targets = [(c, _to_baostock_code(c)) for c in stock_codes]
    targets = [(c, b) for c, b in targets if b]
    if not targets:
        return out

    try:
        from data.fetcher import baostock_session, bs_query_rows
    except Exception as e:
        logger.debug(f"Baostock 未就绪，跳过估值抓取: {e}")
        return out

    start_str = (datetime.now() - timedelta(days=1095)).strftime("%Y-%m-%d")
    end_str = datetime.now().strftime("%Y-%m-%d")
    try:
        with baostock_session(timeout=25.0):
            for ts_code, bs_code in targets:
                try:
                    _, rows = bs_query_rows(
                        bs_code,
                        "date,close,peTTM,pbMRQ,psTTM",
                        start_date=start_str,
                        end_date=end_str,
                        frequency="d",
                    )
                    last_valid = None
                    pos_pes: list[float] = []
                    for row in rows:
                        if row and len(row) >= 4:
                            pe_v = _to_float(row[2])
                            pb_v = _to_float(row[3])
                            if pe_v is not None and pe_v > 0:
                                pos_pes.append(pe_v)
                            if pe_v is not None or pb_v is not None:
                                last_valid = (pe_v, pb_v)
                    if last_valid is not None:
                        cur_pe, cur_pb = last_valid
                        pe_pct = None
                        if cur_pe is not None and cur_pe > 0 and len(pos_pes) >= 20:
                            pe_pct = round(sum(1 for x in pos_pes if x <= cur_pe) / len(pos_pes) * 100.0, 1)
                        out[ts_code] = {
                            "ts_code": ts_code,
                            "pe_ttm": round(cur_pe, 2) if cur_pe is not None else None,
                            "pb": round(cur_pb, 2) if cur_pb is not None else None,
                            "dividend_yield": None,
                            "roe": None,
                            "market_cap_yi": None,
                            "pe_percentile": pe_pct,
                            "next_report_date": "",
                            "updated_at": now_iso,
                        }
                except Exception as e_item:
                    logger.debug(f"Baostock 估值获取跳过 {ts_code}: {e_item}")
    except Exception as e:
        logger.debug(f"Baostock 估值会话异常: {e}")
    return out


def fetch_and_cache_valuations(ts_codes: list[str]) -> dict:
    """尽力抓取并缓存标的估值快照（离线或限流时自动降级返回已有缓存）。"""
    clean_codes = list(dict.fromkeys(str(c).strip() for c in (ts_codes or []) if str(c).strip()))
    if not clean_codes:
        return {}

    stock_codes = [c for c in clean_codes if not _is_etf_code(c)]
    fetched_rows: dict[str, dict] = {}
    now_iso = datetime.now().isoformat()

    # 1. Tushare daily_basic（若已配置 Token）
    if stock_codes:
        try:
            from core.config import TUSHARE_TOKEN
            if TUSHARE_TOKEN and TUSHARE_TOKEN != "your_token_here":
                import tushare as ts
                ts.set_token(TUSHARE_TOKEN)
                pro = ts.pro_api()
                stock_set = set(stock_codes)
                # 尝试近 10 个日历日内最近一个有数据的交易日
                for offset in range(10):
                    dt_str = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
                    df_db = pro.daily_basic(
                        trade_date=dt_str,
                        fields="ts_code,pe_ttm,pb,dv_ttm,dv_ratio,total_mv",
                    )
                    if df_db is not None and not df_db.empty:
                        sub = df_db[df_db["ts_code"].isin(stock_set)]
                        for _, r in sub.iterrows():
                            code = str(r["ts_code"])
                            dv = _to_float(r.get("dv_ttm"))
                            if dv is None:
                                dv = _to_float(r.get("dv_ratio"))
                            mv = _to_float(r.get("total_mv"))
                            fetched_rows[code] = {
                                "ts_code": code,
                                "pe_ttm": _to_float(r.get("pe_ttm")),
                                "pb": _to_float(r.get("pb")),
                                "dividend_yield": dv,
                                "roe": None,
                                "market_cap_yi": round(mv / 10000.0, 2) if mv is not None else None,
                                "pe_percentile": None,
                                "next_report_date": "",
                                "updated_at": now_iso,
                            }
                        break
        except Exception as e:
            logger.debug(f"Tushare 获取估值快照跳过: {e}")

    # 2. Baostock 轻量化逐股快速通道（2秒完成自选股估值，避免触发全市场 59 页爬虫限流）
    missing_codes = [c for c in stock_codes if c not in fetched_rows]
    if missing_codes:
        fetched_rows.update(_fetch_valuations_baostock(missing_codes, now_iso))

    # 3. AKShare 东方财富全市场实时估值快照兜底补缺
    missing_codes = [c for c in stock_codes if c not in fetched_rows]
    if missing_codes:
        try:
            import akshare as ak
            df_spot = ak.stock_zh_a_spot_em()
            if df_spot is not None and not df_spot.empty and "代码" in df_spot.columns:
                sym_to_code = {c.split(".")[0]: c for c in missing_codes}
                df_spot["代码"] = df_spot["代码"].astype(str).str.zfill(6)
                sub = df_spot[df_spot["代码"].isin(sym_to_code)]
                for _, r in sub.iterrows():
                    sym = str(r["代码"])
                    ts_code = sym_to_code.get(sym)
                    if not ts_code:
                        continue
                    pe = _to_float(r.get("市盈率-动态"))
                    pb = _to_float(r.get("市净率"))
                    mv = _to_float(r.get("总市值"))
                    fetched_rows[ts_code] = {
                        "ts_code": ts_code,
                        "pe_ttm": pe,
                        "pb": pb,
                        "dividend_yield": None,
                        "roe": None,
                        "market_cap_yi": round(mv / 1e8, 2) if mv is not None else None,
                        "pe_percentile": None,
                        "next_report_date": "",
                        "updated_at": now_iso,
                    }
        except Exception as e:
            logger.debug(f"AKShare 获取估值快照跳过: {e}")

    if fetched_rows:
        try:
            save_valuation_batch(list(fetched_rows.values()))
        except Exception as e:
            logger.warning(f"写入估值快照失败: {e}")

    return get_valuation_map(clean_codes)


def refresh_watchlist_valuations_if_stale(
    ts_codes: Optional[list[str]] = None,
    max_age_days: int = 7,
) -> dict:
    """定时任务轻量刷新：仅在估值缓存缺失或超过 max_age_days 天时才联网更新，避免每次重复拉全市场快照。"""
    if ts_codes is None:
        try:
            from data.storage import get_watchlist
            wl = get_watchlist()
            ts_codes = wl["ts_code"].tolist() if (wl is not None and not wl.empty) else []
        except Exception:
            ts_codes = []
    stock_codes = [str(c).strip() for c in (ts_codes or []) if str(c).strip() and not _is_etf_code(str(c))]
    if not stock_codes:
        return {}

    existing = get_valuation_map(stock_codes)
    cutoff = datetime.now() - timedelta(days=max(0, int(max_age_days)))
    need_refresh = []
    for code in stock_codes:
        info = existing.get(code)
        if not info or not info.get("updated_at"):
            need_refresh.append(code)
            continue
        if (info.get("pe_ttm") or 0) > 0 and info.get("pe_percentile") is None:
            need_refresh.append(code)
            continue
        try:
            dt_upd = datetime.fromisoformat(str(info["updated_at"])[:19])
            if dt_upd < cutoff:
                need_refresh.append(code)
        except ValueError:
            need_refresh.append(code)

    if need_refresh:
        logger.info(f"自动刷新 {len(need_refresh)} 只自选股估值缓存（超过 {max_age_days} 天或缺失）...")
        return fetch_and_cache_valuations(need_refresh)
    return existing


def evaluate_valuation_guard(
    ts_code: str,
    val_info: Optional[dict] = None,
    is_etf: bool = False,
    ref_date: str = "",
) -> dict:
    """评估单只标的的基本面估值安全度与财报窗口预警。"""
    earnings_warning = False
    if val_info:
        rep = str(val_info.get("next_report_date") or "").replace("-", "").strip()[:8]
        if len(rep) == 8 and rep.isdigit():
            ref_s = (str(ref_date or "").replace("-", "").strip()[:8]
                     or datetime.now().strftime("%Y%m%d"))
            try:
                d_rep = datetime.strptime(rep, "%Y%m%d")
                d_ref = datetime.strptime(ref_s, "%Y%m%d")
                if abs((d_rep - d_ref).days) <= 3:
                    earnings_warning = True
            except ValueError:
                pass

    if is_etf or _is_etf_code(ts_code) or not val_info:
        return {
            "zone": "中性/未缓存",
            "allow_left_buy": True,
            "score_adj": -0.03 if earnings_warning else 0.0,
            "badge": "📅财报窗口" if earnings_warning else "",
            "detail": f"临近财报窗口({val_info.get('next_report_date', '')})" if (earnings_warning and val_info) else "",
            "earnings_warning": earnings_warning,
        }

    pe = _to_float(val_info.get("pe_ttm"))
    pb = _to_float(val_info.get("pb"))
    div_y = _to_float(val_info.get("dividend_yield"))
    pe_pct = _to_float(val_info.get("pe_percentile"))

    if pe is None and pb is None and div_y is None and pe_pct is None:
        return {
            "zone": "中性/未缓存",
            "allow_left_buy": True,
            "score_adj": -0.03 if earnings_warning else 0.0,
            "badge": "📅财报窗口" if earnings_warning else "",
            "detail": "",
            "earnings_warning": earnings_warning,
        }

    is_abs_deep_value = (
        pe is not None and 0 < pe <= 12.0
        and pb is not None and 0 < pb <= 1.2
    )

    if pe is not None and pe < 0:
        zone = "亏损预警"
        allow_left_buy = False
        score_adj = -0.10
        badge = "⚠️亏损(负PE)"
        detail = f"PE(TTM)={pe:.1f}<0，基本面亏损，不宜盲目左侧抄底"
    elif (pe is not None and pe > 60.0 and (pe_pct is None or pe_pct >= 50.0)) or (
        pe_pct is not None and pe_pct >= 85.0 and not is_abs_deep_value
    ):
        zone = "估值偏高"
        allow_left_buy = False
        score_adj = -0.08
        badge = "⚠️高估值"
        pe_txt = f"PE={pe:.1f}" if pe is not None else ""
        pct_txt = f"3年分位={pe_pct:.0f}%" if pe_pct is not None else ""
        detail = f"{' / '.join(x for x in (pe_txt, pct_txt) if x)}，处于高估值拥挤区间，左侧接盘风险大"
    elif (
        ((pe is not None and 0 < pe <= 15.0) or (div_y is not None and div_y >= 3.5))
        and (pe_pct is None or pe_pct < 85.0)
    ) or (pe_pct is not None and 0 < pe_pct <= 30.0):
        zone = "低估高息"
        allow_left_buy = True
        score_adj = 0.03
        badge = "💎低估高息"
        parts = []
        if pe is not None and pe > 0:
            parts.append(f"PE={pe:.1f}")
        if pe_pct is not None:
            parts.append(f"PE分位={pe_pct:.0f}%")
        if div_y is not None and div_y > 0:
            parts.append(f"股息率={div_y:.2f}%")
        detail = "安全边际充足" + (f"（{'，'.join(parts)}）" if parts else "")
    else:
        zone = "估值合理"
        allow_left_buy = True
        score_adj = 0.0
        badge = "✅估值合理"
        parts = []
        if pe is not None:
            parts.append(f"PE={pe:.1f}")
        if pb is not None:
            parts.append(f"PB={pb:.2f}")
        if pe_pct is not None:
            parts.append(f"PE分位={pe_pct:.0f}%")
        detail = "，".join(parts)

    if earnings_warning:
        score_adj = round(score_adj - 0.03, 2)
        badge = f"{badge} 📅财报窗口".strip()
        detail = f"{detail}；临近财报窗口({val_info.get('next_report_date', '')})注意波动".lstrip("；")

    return {
        "zone": zone,
        "allow_left_buy": allow_left_buy,
        "score_adj": round(score_adj, 2),
        "badge": badge,
        "detail": detail,
        "earnings_warning": earnings_warning,
    }


def _estimate_causal_atr_and_close(ts_code: str, daily_loader) -> tuple[float, float]:
    """用最近 60 根已收盘 K 线估计标的中位 ATR% 与最新收盘价（严格因果）。"""
    try:
        df = daily_loader(ts_code, limit=90)
    except Exception:
        return 2.2, 0.0
    if df is None or df.empty:
        return 2.2, 0.0
    latest_close = _to_float(df.iloc[-1].get("close")) or 0.0
    if len(df) >= 15 and all(c in df.columns for c in ("high", "low", "close")):
        df_atr = add_atr(df, period=14)
        valid_atr = df_atr["atr_pct"].dropna().tail(60)
        if not valid_atr.empty:
            med = float(valid_atr.median())
            if math.isfinite(med) and med > 0:
                return round(med, 2), latest_close
    return 2.2, latest_close


def build_core_satellite_plan(
    watchlist_df: pd.DataFrame,
    capital: float = 100000.0,
    core_ratio: float = 0.70,
    val_map: Optional[dict] = None,
    inst_df: Optional[pd.DataFrame] = None,
    daily_loader=None,
) -> dict:
    """生成「核心底仓 70% + 卫星趋势 30%」动态配置计划。"""
    cap = max(0.0, float(capital or 0.0))
    ratio = min(max(float(core_ratio), 0.0), 1.0)
    core_budget = round(cap * ratio, 2)
    satellite_budget = round(cap * (1.0 - ratio), 2)

    if watchlist_df is None or watchlist_df.empty:
        return {
            "rows": [],
            "core_budget": core_budget,
            "satellite_budget": satellite_budget,
            "cash_reserve": cap,
            "summary": "自选股为空，暂无可配置标的。",
        }

    val_lookup = val_map if val_map is not None else get_valuation_map(watchlist_df["ts_code"].tolist())
    if inst_df is None:
        try:
            inst_df = get_instrument_list()
        except Exception:
            inst_df = pd.DataFrame()
    name_map = dict(zip(inst_df["ts_code"], inst_df["name"])) if (inst_df is not None and not inst_df.empty) else {}
    type_map = dict(zip(inst_df["ts_code"], inst_df["type"])) if (inst_df is not None and not inst_df.empty and "type" in inst_df.columns) else {}
    loader = daily_loader or get_daily

    items = []
    for _, r in watchlist_df.iterrows():
        code = str(r["ts_code"]).strip()
        if not code:
            continue
        rec_tag = str(r.get("recommended_strategy") or "").strip()
        no_trade = ("暂不适配" in rec_tag) or ("⛔" in rec_tag)
        is_etf = (type_map.get(code) == "ETF") or _is_etf_code(code)
        atr_pct, latest_close = _estimate_causal_atr_and_close(code, loader)
        val_info = val_lookup.get(code, {})
        val_guard = evaluate_valuation_guard(code, val_info, is_etf=is_etf)
        div_y = _to_float(val_info.get("dividend_yield"))
        pe_ttm = _to_float(val_info.get("pe_ttm"))

        high_div_low_vol = (div_y is not None and div_y >= 3.5 and atr_pct < 2.2)

        if (no_trade and not is_etf and not high_div_low_vol) or val_guard["zone"] == "亏损预警":
            role = "⛔ 观望/不配"
            action_note = "样本外暂不适配或基本面亏损，保留现金观望"
        elif is_etf or atr_pct < 2.2 or high_div_low_vol:
            role = "🧱 核心底仓"
            action_note = "建议 70% 长持底仓 + 30% 低吸波段高抛，不追高"
        else:
            role = "⚡ 卫星趋势"
            action_note = "严格跟随趋势买点+熊市择时，按 2.2×ATR 移动止盈"

        inv_vol_weight = 1.0 / max(atr_pct, 1.0)
        items.append({
            "ts_code": code,
            "name": name_map.get(code, ""),
            "is_etf": is_etf,
            "atr_pct": atr_pct,
            "latest_close": latest_close,
            "pe_ttm": pe_ttm,
            "dividend_yield": div_y,
            "val_badge": val_guard["badge"] or ("宽基ETF" if is_etf else "未拉取"),
            "val_zone": val_guard["zone"],
            "rec_tag": rec_tag or "-",
            "role": role,
            "inv_vol_weight": inv_vol_weight,
            "action_note": action_note,
        })

    core_items = [x for x in items if x["role"] == "🧱 核心底仓"]
    sat_items = [x for x in items if x["role"] == "⚡ 卫星趋势"]
    core_w_sum = sum(x["inv_vol_weight"] for x in core_items) or 1.0
    sat_w_sum = sum(x["inv_vol_weight"] for x in sat_items) or 1.0

    allocated_total = 0.0
    rows = []
    for x in items:
        if x["role"] == "🧱 核心底仓":
            alloc = core_budget * (x["inv_vol_weight"] / core_w_sum)
            sl_pct = round(min(max(x["atr_pct"] * 3.0, 4.0), 10.0), 1)
        elif x["role"] == "⚡ 卫星趋势":
            alloc = satellite_budget * (x["inv_vol_weight"] / sat_w_sum)
            sl_pct = round(min(max(x["atr_pct"] * 2.2, 3.5), 8.0), 1)
        else:
            alloc = 0.0
            sl_pct = 0.0

        px = x["latest_close"]
        shares = int(alloc / px) // 100 * 100 if (alloc > 0 and px > 0) else 0
        actual_amt = round(shares * px, 2) if (shares > 0 and px > 0) else round(alloc, 2)
        if shares > 0:
            allocated_total += actual_amt
        stop_px = round(px * (1.0 - sl_pct / 100.0), 2) if (px > 0 and sl_pct > 0) else 0.0
        target_pct = round(alloc / cap * 100.0, 1) if cap > 0 else 0.0

        rows.append({
            "代码": x["ts_code"],
            "名称": x["name"],
            "资产角色": x["role"],
            "60日ATR%": x["atr_pct"],
            "现价": round(px, 2) if px > 0 else 0.0,
            "估值状态": x["val_badge"],
            "推荐策略": x["rec_tag"],
            "目标仓位%": target_pct,
            "分配资金(元)": round(alloc, 0),
            "建议股数": shares,
            "ATR止损价": stop_px if stop_px > 0 else "-",
            "操作纪律": x["action_note"],
        })

    cash_reserve = round(max(0.0, cap - allocated_total), 2)
    skip_cnt = sum(1 for x in items if x["role"] == "⛔ 观望/不配")
    summary = (
        f"核心底仓 {len(core_items)} 只（预算 ¥{core_budget:,.0f}） | "
        f"卫星趋势 {len(sat_items)} 只（预算 ¥{satellite_budget:,.0f}） | "
        f"观望现金池 {skip_cnt} 只（预留现金约 ¥{cash_reserve:,.0f}）"
    )
    return {
        "rows": rows,
        "core_budget": core_budget,
        "satellite_budget": satellite_budget,
        "cash_reserve": cash_reserve,
        "core_count": len(core_items),
        "satellite_count": len(sat_items),
        "skip_count": skip_cnt,
        "summary": summary,
    }
