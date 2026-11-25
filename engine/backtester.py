"""回测引擎主循环"""
import json
import logging
import time
from functools import lru_cache
from typing import List, Type, Callable
import pandas as pd
import itertools
import concurrent.futures

from engine.portfolio import Portfolio
from engine.commission import cap_order_volume
from engine.bar_view import BarFrame, FrameStore
from data.storage import get_daily, save_backtest_result, save_backtest_trades
from data.indicators import apply_indicators
from data.cleaner import clean_daily
from core.models import BacktestResult, Trade
from core.config import (
    BACKTEST_MIN_POSITION_PCT, BACKTEST_POSITION_STEP,
    BACKTEST_ST_CACHE_TTL, BACKTEST_BENCHMARK_CACHE_SIZE,
    BACKTEST_FAST_BAR_VIEW,
)

logger = logging.getLogger(__name__)


# ============================================================
# 回测引擎配置 —— 单次回测 / 网格搜索 / 样本外验证必须共用同一套撮合规则
# ============================================================
# 网格搜索和 walk_forward 过去只把 strategy/params/dates 传给 Backtester，
# 把下面这些参数留在默认值上。结果就是：用户在单次回测里选了 ATR 仓位、
# 持仓上限、大盘择时，选参阶段却按「评分定仓 + 不限持仓 + 不择时」跑，
# 挑出来的「最优参数」与实盘配置根本不是一回事。
BACKTEST_OPTION_KEYS = (
    "max_active_positions", "position_sizing", "risk_per_trade",
    "market_regime_filter", "market_index",
    "max_open_gap_pct", "drawdown_brake_pct",
    "max_sector_positions", "max_correlation", "dual_index_regime",
    "bear_position_scale", "cash_yield_annual",
)


def build_backtest_options(max_active_positions: int = 0,
                           position_sizing: str = "score",
                           risk_per_trade: float = 0.015,
                           market_regime_filter: bool = False,
                           market_index: str = "000300.SH",
                           max_open_gap_pct: float = 0.0,
                           drawdown_brake_pct: float = 0.0,
                           max_sector_positions: int = 0,
                           max_correlation: float = 0.0,
                           dual_index_regime: bool = False,
                           bear_position_scale: float = 1.0,
                           cash_yield_annual: float = 0.0) -> dict:
    """把影响撮合结果的引擎配置打包，交给 grid_search / walk_forward 复用。"""
    return {
        "max_active_positions": int(max_active_positions),
        "position_sizing": position_sizing,
        "risk_per_trade": float(risk_per_trade),
        "market_regime_filter": bool(market_regime_filter),
        "market_index": market_index,
        "max_open_gap_pct": float(max_open_gap_pct),
        "drawdown_brake_pct": float(drawdown_brake_pct),
        "max_sector_positions": int(max_sector_positions),
        "max_correlation": float(max_correlation),
        "dual_index_regime": bool(dual_index_regime),
        "bear_position_scale": float(bear_position_scale),
        "cash_yield_annual": float(cash_yield_annual),
    }


def _options_kwargs(backtest_options: dict) -> dict:
    """只展开 Backtester 认识的配置键，忽略调用方塞进来的其它信息。"""
    if not backtest_options:
        return {}
    return {k: v for k, v in backtest_options.items() if k in BACKTEST_OPTION_KEYS}


def _liquidity_context(fa, idx):
    """取 (参考成交量, 日波动率) 供参与率上限与冲击成本使用。

    idx 必须是撮合时点已经收盘的那一行：next_open 用信号日（curr_idx - 1），
    current_close 可用当日。绝不能读执行日尚未收盘的 volume / atr_pct。
    """
    if fa is None or idx is None or idx < 0:
        return 0.0, 0.0
    vol = fa.val("volume", idx, 0.0) or 0.0
    atr_pct = fa.val("atr_pct", idx, 0.0) or 0.0
    return float(vol), float(atr_pct) / 100.0


def _resolve_sell_volume(avail_before: int, sig, ref_volume: float) -> tuple[int, int]:
    """根据信号的 sell_ratio 与市场参与率上限计算 (目标卖出股数, 实际下单股数)。"""
    snap = getattr(sig, "context_snapshot", None) or {}
    raw_ratio = snap.get("sell_ratio", 1.0)
    try:
        ratio = float(raw_ratio)
    except (TypeError, ValueError):
        ratio = 1.0
    if 0.0 < ratio < 1.0:
        desired_sell = int(avail_before * ratio) // 100 * 100
        if desired_sell <= 0:
            desired_sell = avail_before
    else:
        desired_sell = avail_before
    sell_vol = cap_order_volume(desired_sell, ref_volume, lot_size=1)
    return desired_sell, sell_vol


# ============================================================
# 进程内缓存 —— 消除网格搜索 / 批量回测中的重复 I/O
# ============================================================

_ST_CACHE = {"ts": 0.0, "lookup": {}}
_INDUSTRY_CACHE = {"ts": 0.0, "lookup": {}}


def get_st_lookup(force_refresh: bool = False) -> dict:
    """获取 {ts_code: is_st} 映射，进程内带 TTL 缓存。

    回测主循环此前每次 run() 都全量拉取标的列表并 iterrows 建字典，
    网格搜索 N 个参数组合就是 N 次重复解析。这里做进程级缓存，
    标的名称变更（如戴帽/摘帽）在 TTL 内可能滞后，可通过 force_refresh 强制刷新。
    """
    now = time.time()
    if (not force_refresh and _ST_CACHE["lookup"]
            and now - _ST_CACHE["ts"] < BACKTEST_ST_CACHE_TTL):
        return _ST_CACHE["lookup"]

    from data.storage import get_instrument_list
    lookup: dict = {}
    try:
        instruments = get_instrument_list()
    except Exception as e:
        logger.warning(f"标的列表加载失败，ST 判定全部按非 ST 处理: {e}")
        instruments = None

    if instruments is not None and not instruments.empty:
        codes = instruments["ts_code"].tolist()
        # is_st 字段优先，其次按名称含 "ST" 兜底；两者任一命中即为 ST
        if "is_st" in instruments.columns:
            flag_col = instruments["is_st"].fillna(0).astype(int).eq(1).tolist()
        else:
            flag_col = [False] * len(codes)
        if "name" in instruments.columns:
            name_col = (instruments["name"].astype(str).str.upper()
                        .str.contains("ST", na=False).tolist())
        else:
            name_col = [False] * len(codes)
        lookup = {c: bool(a or b) for c, a, b in zip(codes, flag_col, name_col)}

    _ST_CACHE["lookup"] = lookup
    _ST_CACHE["ts"] = now
    return lookup


def get_industry_lookup(force_refresh: bool = False) -> dict:
    """获取 {ts_code: industry} 行业分类缓存映射，供同板块持仓上限使用。"""
    now = time.time()
    if (not force_refresh and _INDUSTRY_CACHE["lookup"]
            and now - _INDUSTRY_CACHE["ts"] < BACKTEST_ST_CACHE_TTL):
        return _INDUSTRY_CACHE["lookup"]

    from data.storage import get_instrument_list
    lookup: dict = {}
    try:
        instruments = get_instrument_list()
        if instruments is not None and not instruments.empty and "industry" in instruments.columns:
            codes = instruments["ts_code"].tolist()
            inds = instruments["industry"].fillna("").astype(str).str.strip().tolist()
            lookup = {str(c): ind for c, ind in zip(codes, inds) if ind}
    except Exception as e:
        logger.debug(f"行业映射加载失败，跳过行业分散度限制: {e}")

    _INDUSTRY_CACHE["lookup"] = lookup
    _INDUSTRY_CACHE["ts"] = now
    return lookup


def clear_backtester_cache():
    """清空引擎级缓存（标的信息变更后调用，测试亦可用它隔离状态）"""
    _ST_CACHE["lookup"] = {}
    _ST_CACHE["ts"] = 0.0
    _INDUSTRY_CACHE["lookup"] = {}
    _INDUSTRY_CACHE["ts"] = 0.0
    get_benchmark_returns.cache_clear()
    _preload_cached.cache_clear()


@lru_cache(maxsize=BACKTEST_BENCHMARK_CACHE_SIZE)
def get_benchmark_returns(start_date: str, end_date: str) -> dict:
    """获取基准指数日收益 {trade_date: ret}，LRU 缓存。

    失败返回空字典，由调用方降级处理（Alpha/Beta 置零）。
    """
    import numpy as np
    from data.storage import get_index_daily_range

    for bm_code in ("000300.SH", "000001.SH"):
        try:
            bm_df = get_index_daily_range(bm_code, start_date, end_date)
        except Exception as e:
            logger.warning(f"基准 {bm_code} 加载失败: {e}")
            continue
        if bm_df.empty or len(bm_df) < 2 or "close" not in bm_df.columns:
            continue
        bm_df = bm_df.sort_values("trade_date")
        pct = bm_df["close"].astype(float).pct_change()
        dates = bm_df["trade_date"].astype(str).str.replace("-", "")
        bm_map = {d: float(r) for d, r in zip(dates, pct)
                  if pd.notna(r) and np.isfinite(r)}
        if bm_map:
            return bm_map
    return {}


class _StockArrays:
    """单只标的的列式 numpy 视图。

    回测主循环此前用 ``df.iloc[idx]["close"]`` 取当日收盘价 —— 在 30 余列的
    宽表上每次都会构造一整行 Series 再取单值，是纯浪费。这里复用 ``FrameStore``
    的惰性列数组，主循环按整数下标直接读取，避免任何 Series 构造开销。
    策略仍拿到原始 DataFrame 视图，接口完全不变。
    """
    __slots__ = ("ts_code", "n", "dates", "_row_of", "_store",
                 "is_st", "is_cy", "is_bj")

    # 主循环与快照所需的列
    FAST_COLS = ("open", "close", "high", "low", "ma5", "ma20", "ma60",
                 "rsi14", "dif", "dea", "kdj_k", "atr14",
                 "volume", "atr_pct")  # 流动性约束与冲击成本所需的列

    def __init__(self, ts_code: str, df: pd.DataFrame, st_lookup: dict,
                 store: FrameStore = None):
        self.ts_code = ts_code
        self.n = len(df)
        self.dates = df["trade_date"].tolist()
        self._row_of = {d: i for i, d in enumerate(self.dates)}
        self._store = store if store is not None else FrameStore(df)
        self.is_st = st_lookup.get(ts_code, False)
        self.is_cy = ts_code.startswith(("300", "301", "688"))
        self.is_bj = ts_code.startswith(("43", "83", "87", "92")) or ts_code.endswith(".BJ")

    def row_of(self, date: str):
        """返回该交易日对应的行下标，缺失（停牌）返回 None"""
        return self._row_of.get(date)

    def val(self, col: str, i: int, default=None):
        """读取指定列第 i 行的标量，越界或 NaN 返回 default"""
        if i is None or i < 0 or i >= self.n or col not in self._store.columns:
            return default
        v = self._store.array(col)[i]
        # NaN 判定（含 numpy float）
        return default if v != v else float(v)


class _SubPortfolioView:
    """按标的子集隔离暴露 positions 的轻量视图（供多策略路由风控分桶使用）。"""
    __slots__ = ("positions",)

    def __init__(self, portfolio: Portfolio, allowed_codes: set):
        self.positions = {
            c: p for c, p in portfolio.positions.items()
            if c in allowed_codes and not p.is_empty
        }


def _build_index_regime_map(index_code: str, start_date: str, end_date: str, fallback_sh: bool = False) -> dict:
    """加载指数并构建 {trade_date: allow_buy} 牛熊映射表（复用 signal_service.classify_index_regime）。"""
    regime_map: dict = {}
    try:
        from data.storage import get_index_daily_range
        from services.signal_service import classify_index_regime
        from datetime import datetime, timedelta
        idx_warmup = start_date
        if start_date:
            try:
                idx_dt = datetime.strptime(start_date, "%Y%m%d")
                idx_warmup = (idx_dt - timedelta(days=180)).strftime("%Y%m%d")
            except Exception:
                idx_warmup = start_date
        bm_df = get_index_daily_range(index_code, idx_warmup, end_date)
        if fallback_sh and (bm_df.empty or len(bm_df) < 20):
            bm_df = get_index_daily_range("000001.SH", idx_warmup, end_date)
        if not bm_df.empty and len(bm_df) >= 20:
            bm_df = clean_daily(bm_df)
            bm_df = apply_indicators(bm_df, ["ma", "rsi"])
            ma20_vals = bm_df["ma20"].tolist() if "ma20" in bm_df.columns else []
            for idx_i, (_, row) in enumerate(bm_df.iterrows()):
                d_str = str(row["trade_date"]).replace("-", "").strip()
                c = float(row["close"])
                raw_m20 = row.get("ma20", None)
                m20 = float(raw_m20) if (raw_m20 is not None and pd.notna(raw_m20)) else c
                if ma20_vals and idx_i >= 2 and pd.notna(ma20_vals[idx_i - 2]):
                    prev_m20 = float(ma20_vals[idx_i - 2])
                else:
                    prev_m20 = m20
                raw_m60 = row.get("ma60", None)
                m60 = float(raw_m60) if (raw_m60 is not None and pd.notna(raw_m60)) else m20
                raw_rsi = row.get("rsi14", None)
                rsi14 = float(raw_rsi) if (raw_rsi is not None and pd.notna(raw_rsi)) else 50.0
                regime_map[d_str] = classify_index_regime(c, m20, prev_m20, m60, rsi14) != "BEAR"
    except Exception as e:
        logger.warning(f"指数 {index_code} 择时映射初始化失败: {e}")
    return regime_map


def _exceeds_correlation_limit(
    ts_code: str,
    cutoff_date: str,
    portfolio: Portfolio,
    fast_data: dict,
    max_corr: float,
    lookback: int = 60,
) -> bool:
    """检测拟新开仓标的截至 cutoff_date 的近60日收益与当前已有持仓是否存在超限同向共振。"""
    import bisect
    import numpy as np

    if max_corr <= 0 or not cutoff_date:
        return False
    held_codes = [c for c, p in portfolio.positions.items() if not p.is_empty and c != ts_code]
    if not held_codes:
        return False
    fa_cand = fast_data.get(ts_code)
    if fa_cand is None:
        return False

    def _ret_dict(fa) -> dict:
        end_i = bisect.bisect_right(fa.dates, cutoff_date) - 1
        if end_i < 20:
            return {}
        start_i = max(0, end_i - lookback)
        closes = fa._store.array("close")[start_i:end_i + 1]
        ds = fa.dates[start_i:end_i + 1]
        out = {}
        for k in range(1, len(closes)):
            c0, c1 = float(closes[k - 1]), float(closes[k])
            if c0 > 0 and c1 == c1 and c0 == c0:
                out[ds[k]] = (c1 / c0) - 1.0
        return out

    cand_rets = _ret_dict(fa_cand)
    if len(cand_rets) < 20:
        return False

    for h_code in held_codes:
        fa_h = fast_data.get(h_code)
        if fa_h is None:
            continue
        h_rets = _ret_dict(fa_h)
        common_dates = [d for d in cand_rets if d in h_rets]
        if len(common_dates) < 20:
            continue
        x = np.array([cand_rets[d] for d in common_dates], dtype=float)
        y = np.array([h_rets[d] for d in common_dates], dtype=float)
        sx, sy = np.std(x), np.std(y)
        if sx > 1e-9 and sy > 1e-9:
            corr = float(np.corrcoef(x, y)[0, 1])
            if corr == corr and corr > max_corr:
                return True
    return False


def _parse_router_map(raw_router: dict, fallback_cls=None, fallback_params: dict = None, registry: dict = None) -> dict:
    """将 {ts_code: str | cls | (cls_or_str, params)} 统一解析为 {ts_code: (s_cls, s_params, base_name, g_key)}。"""
    out = {}
    for code, spec in (raw_router or {}).items():
        s_params = {}
        if spec is None:
            if fallback_cls is None:
                continue
            s_cls = fallback_cls
            s_params = dict(fallback_params or {})
            base_name = getattr(s_cls, "name", "custom")
        elif isinstance(spec, (tuple, list)) and len(spec) == 2:
            s_spec, raw_p = spec
            s_params = dict(raw_p or {})
            if isinstance(s_spec, str):
                if registry is None or s_spec not in registry:
                    continue
                s_cls = registry[s_spec]
                base_name = s_spec
            else:
                s_cls = s_spec
                base_name = getattr(s_cls, "name", str(s_cls))
        elif isinstance(spec, str):
            if registry is None or spec not in registry:
                continue
            s_cls = registry[spec]
            base_name = spec
        else:
            s_cls = spec
            base_name = getattr(s_cls, "name", str(s_cls))

        p_sig = json.dumps(s_params, sort_keys=True, default=str) if s_params else ""
        g_key = f"{base_name}:{p_sig}" if p_sig else base_name
        out[str(code)] = (s_cls, s_params, base_name, g_key)
    return out


class Backtester:
    """回测引擎

    用法:
        bt = Backtester(strategy_cls, params, universe, start_date, end_date, capital)
        result = bt.run()
    """

    def __init__(self, strategy_cls, params: dict = None,
                 universe: list = None, start_date: str = "", end_date: str = "",
                 initial_capital: float = 100000,
                 execution_mode: str = "current_close",
                 risk_manager = None,
                 preloaded_data: dict = None,
                 max_active_positions: int = 0,
                 position_sizing: str = "score",
                 risk_per_trade: float = 0.015,
                 market_regime_filter: bool = False,
                 market_index: str = "000300.SH",
                 max_open_gap_pct: float = 0.0,
                 drawdown_brake_pct: float = 0.0,
                 max_sector_positions: int = 0,
                 max_correlation: float = 0.0,
                 dual_index_regime: bool = False,
                 strategy_router: dict = None,
                 bear_position_scale: float = 1.0,
                 cash_yield_annual: float = 0.0,
                 router_schedule: dict = None):
        self.strategy_cls = strategy_cls
        self.params = params or {}
        self.universe = list(universe or [])
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.execution_mode = execution_mode
        self.risk_manager = risk_manager
        self.preloaded_data = preloaded_data
        self.max_active_positions = max_active_positions
        self.position_sizing = position_sizing
        self.risk_per_trade = risk_per_trade
        self.market_regime_filter = market_regime_filter
        self.market_index = market_index
        self.max_open_gap_pct = max(0.0, float(max_open_gap_pct or 0.0))
        self.drawdown_brake_pct = max(0.0, float(drawdown_brake_pct or 0.0))
        self.max_sector_positions = max(0, int(max_sector_positions or 0))
        self.max_correlation = max(0.0, float(max_correlation or 0.0))
        self.dual_index_regime = bool(dual_index_regime)
        self.strategy_router = dict(strategy_router) if strategy_router else None
        self.bear_position_scale = min(1.0, max(0.0, float(bear_position_scale if bear_position_scale is not None else 1.0)))
        self.cash_yield_annual = max(0.0, float(cash_yield_annual or 0.0))
        self.router_schedule = dict(router_schedule) if router_schedule else None

    def _is_bear_half_scale(self, date: str, ts_code: str,
                            fa=None, decision_idx: int = None) -> bool:
        """大盘或创业板空头期对放行的超卖反转买单实施仓位缩放。"""
        if not self.market_regime_filter or not (0.0 < self.bear_position_scale < 1.0):
            return False
        if not self.market_regime_lookup.get(date, True):
            return True
        if self.dual_index_regime and not getattr(self, "growth_regime_lookup", {}).get(date, True):
            is_cy = fa.is_cy if fa is not None else ts_code.startswith(("300", "301", "688"))
            atr_v = fa.val("atr_pct", decision_idx, 0.0) if (fa is not None and decision_idx is not None) else 0.0
            if is_cy or (atr_v is not None and atr_v >= 2.2):
                return True
        return False

    def _is_buy_regime_allowed(self, date: str, ts_code: str, allow_bear_buy: bool,
                               fa=None, decision_idx: int = None) -> bool:
        """统一判断指定标的在当日大盘/双指数环境下是否允许开多仓。"""
        if allow_bear_buy or not self.market_regime_filter:
            return True
        if not self.market_regime_lookup.get(date, True):
            return False
        if self.dual_index_regime and not getattr(self, "growth_regime_lookup", {}).get(date, True):
            is_cy = fa.is_cy if fa is not None else ts_code.startswith(("300", "301", "688"))
            atr_v = fa.val("atr_pct", decision_idx, 0.0) if (fa is not None and decision_idx is not None) else 0.0
            if is_cy or (atr_v is not None and atr_v >= 2.2):
                return False
        return True

    def _can_open_position(self, ts_code: str, portfolio: Portfolio,
                           cutoff_date: str, fast_data: dict) -> bool:
        """检查组合层级的仓位上限、行业分散度与高相关性防抱团约束。"""
        curr_pos = portfolio.get_position(ts_code)
        is_new_pos = (curr_pos is None or curr_pos.is_empty)
        if not is_new_pos:
            return True
        if self.max_active_positions > 0 and portfolio.active_position_count >= self.max_active_positions:
            return False
        if self.max_sector_positions > 0:
            sec = getattr(self, "industry_lookup", {}).get(ts_code, "")
            if sec:
                sec_cnt = sum(
                    1 for c, p in portfolio.positions.items()
                    if not p.is_empty and getattr(self, "industry_lookup", {}).get(c, "") == sec
                )
                if sec_cnt >= self.max_sector_positions:
                    return False
        if self.max_correlation > 0:
            if _exceeds_correlation_limit(ts_code, cutoff_date, portfolio, fast_data, self.max_correlation):
                return False
        return True

    def run(self, save: bool = True) -> BacktestResult:
        """执行回测

        Returns:
            BacktestResult对象
        """
        # 1. 初始化 ST 与行业缓存，扩展加载范围覆盖完整日程路由
        self.st_lookup = get_st_lookup()
        self.industry_lookup = get_industry_lookup() if self.max_sector_positions > 0 else {}
        strat_label = (
            self.strategy_cls.name if self.strategy_cls is not None
            else ("rolling_adaptive_router" if self.router_schedule else "adaptive_router")
        )

        load_universe = list(self.universe)
        if self.router_schedule:
            seen_u = set(load_universe)
            for _eff_dt, r_map in self.router_schedule.items():
                for c in (r_map or {}):
                    if c not in seen_u:
                        seen_u.add(c)
                        load_universe.append(c)

        # 2. 加载并预处理所有股票数据（若已传入 preloaded_data 则免重复 I/O）
        if self.preloaded_data is not None and self.preloaded_data:
            stock_data = {
                ts_code: self.preloaded_data[ts_code]
                for ts_code in load_universe
                if ts_code in self.preloaded_data and not self.preloaded_data[ts_code].empty
            }
        else:
            logger.info(f"加载数据: {len(load_universe)} 只股票, "
                         f"{self.start_date} ~ {self.end_date}")
            stock_data = {}  # {ts_code: DataFrame}
            # 计算 warm-up 起始日期（提前180天加载历史K线，确保MA60/MACD/KDJ等长周期指标在回测首日就已就绪）
            warmup_start = ""
            if self.start_date:
                try:
                    from datetime import datetime, timedelta
                    dt = datetime.strptime(self.start_date, "%Y%m%d")
                    warmup_start = (dt - timedelta(days=180)).strftime("%Y%m%d")
                except Exception:
                    warmup_start = self.start_date

            for ts_code in load_universe:
                df = get_daily(ts_code, warmup_start, self.end_date)
                if df.empty:
                    continue
                df = clean_daily(df)
                if df.empty:
                    continue
                df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
                stock_data[ts_code] = df

        if not stock_data:
            logger.error("没有可用数据")
            return BacktestResult(
                strategy=strat_label, params=json.dumps(self.params),
                start_date=self.start_date, end_date=self.end_date,
                initial_capital=self.initial_capital, final_capital=self.initial_capital,
            )

        logger.info(f"有效股票: {len(stock_data)} 只")

        # 2. 构建所有交易日序列
        all_dates = set()
        for df in stock_data.values():
            all_dates.update(df["trade_date"].tolist())
        trade_dates = sorted(all_dates)

        # 只取 start_date ~ end_date 之间的
        trade_dates = [d for d in trade_dates
                       if self.start_date <= d <= self.end_date]
        logger.info(f"交易日数: {len(trade_dates)}")

        # 3. 初始化策略（单策略 / 静态路由 / 连续日程热切换路由）和组合
        use_routing = bool(self.router_schedule or self.strategy_router)
        strategy = self.strategy_cls(**self.params) if (self.strategy_cls is not None and not use_routing) else None
        router_groups: dict = {}
        code_strategy_key: dict = {}
        schedule_entries: list = []
        sched_strat_pool: dict = {}
        sched_rm_pool: dict = {}
        self.sched_rm_pool = sched_rm_pool
        active_code_group: dict = {}
        position_owner_group: dict = {}

        if use_routing:
            from strategies import STRATEGY_REGISTRY
            raw_sched = self.router_schedule if self.router_schedule else {(self.start_date or "00000000"): self.strategy_router}
            for eff_dt in sorted(raw_sched.keys()):
                parsed = _parse_router_map(raw_sched[eff_dt], self.strategy_cls, self.params, STRATEGY_REGISTRY)
                parsed_in_data = {c: v for c, v in parsed.items() if c in stock_data}
                schedule_entries.append((str(eff_dt), parsed_in_data))
                for _c, (s_cls, s_params, base_name, g_key) in parsed_in_data.items():
                    if g_key not in sched_strat_pool:
                        sched_strat_pool[g_key] = (s_cls(**s_params), base_name)

            if not schedule_entries and not self.router_schedule and self.strategy_cls is not None:
                strategy = self.strategy_cls(**self.params)

        def _apply_active_router(parsed_map: dict):
            nonlocal router_groups, code_strategy_key, active_code_group
            router_groups = {}
            code_strategy_key = {}
            active_code_group = {}
            for code_k, (_sc, _sp, base_n, g_key) in parsed_map.items():
                code_strategy_key[code_k] = g_key
                active_code_group[code_k] = (g_key, base_n)
                s_inst = sched_strat_pool[g_key][0]
                if g_key not in router_groups:
                    router_groups[g_key] = (s_inst, [])
                router_groups[g_key][1].append(code_k)

        sched_cursor = 0
        if schedule_entries and not self.router_schedule:
            _apply_active_router(schedule_entries[0][1])
            sched_cursor = len(schedule_entries)

        # 自动匹配风控实例（当 risk_manager == "auto" 时按路由策略或主策略生成独立风控，跨日程连续持久化）
        active_risk_manager = self.risk_manager
        auto_rm_groups: dict = {}
        self.auto_rm_groups = auto_rm_groups
        self.sched_rm_pool = sched_rm_pool
        if self.risk_manager == "auto":
            from engine.risk_manager import RiskManager
            if sched_strat_pool:
                for _g_key, (_s_inst, base_name) in sched_strat_pool.items():
                    if base_name not in sched_rm_pool:
                        sched_rm_pool[base_name] = RiskManager.for_strategy(base_name, use_atr_stop=True)
                for b_name, rm_inst in sched_rm_pool.items():
                    auto_rm_groups[b_name] = (rm_inst, set())
                for c_k, (_gk, b_n) in active_code_group.items():
                    if b_n in auto_rm_groups:
                        auto_rm_groups[b_n][1].add(c_k)
                active_risk_manager = None
            else:
                active_risk_manager = RiskManager.for_strategy(strat_label, use_atr_stop=True)

        portfolio = Portfolio(self.initial_capital)

        # 预先按日期索引数据（加速查找）
        # 向量化构建：此前用 iterrows 逐行遍历全量K线（含180天warmup），
        # 单次回测就要 2.7s —— 占整个 run() 的 30%，且与策略复杂度无关。
        date_index = {}  # {ts_code: {trade_date: row_index}}
        for ts_code, df in stock_data.items():
            date_index[ts_code] = dict(zip(df["trade_date"].tolist(), range(len(df))))

        # 列式 numpy 视图：主循环取当前价/开盘价/前收/快照字段时零 Series 构造
        frame_stores = {ts_code: FrameStore(df) for ts_code, df in stock_data.items()}
        fast_data = {
            ts_code: _StockArrays(ts_code, df, self.st_lookup, store=frame_stores[ts_code])
            for ts_code, df in stock_data.items()
        }
        use_fast_view = BACKTEST_FAST_BAR_VIEW

        pending_signals = []

        # 初始化大盘牛熊择时网关（含可选双指数创业板成长择时）
        self.market_regime_lookup = {}
        self.growth_regime_lookup = {}
        if self.market_regime_filter:
            self.market_regime_lookup = _build_index_regime_map(
                self.market_index, self.start_date, self.end_date, fallback_sh=True
            )
            if self.dual_index_regime:
                self.growth_regime_lookup = _build_index_regime_map(
                    "399006.SZ", self.start_date, self.end_date, fallback_sh=False
                )

        # 4. 逐日迭代
        single_allow_bear = bool(getattr(strategy, "allow_bear_buy", False)) if strategy is not None else False
        pending_deferred_sells = []
        equity_peak = float(self.initial_capital)
        brake_active = False
        for date in trade_dates:
            # 每日开盘前触发持仓日结（T+1结转）
            portfolio.on_new_day(date)

            # 连续滚动热切换：到达新周期调仓日时无缝更新当前激活路由（保留已开仓持仓原属权与风控延续）
            while sched_cursor < len(schedule_entries) and schedule_entries[sched_cursor][0] <= date:
                _apply_active_router(schedule_entries[sched_cursor][1])
                if sched_rm_pool:
                    for b_n, (rm_inst, c_set) in auto_rm_groups.items():
                        c_set.clear()
                    for c_k, (_gk, b_n) in active_code_group.items():
                        if b_n in auto_rm_groups:
                            auto_rm_groups[b_n][1].add(c_k)
                sched_cursor += 1

            # 更新因果账户回撤降速器状态（严格基于昨日已收盘净值，零未来函数）
            if self.drawdown_brake_pct > 0:
                last_eq = portfolio.equity_curve[-1]["equity"] if portfolio.equity_curve else float(self.initial_capital)
                equity_peak = max(equity_peak, last_eq)
                curr_dd = ((equity_peak - last_eq) / equity_peak * 100.0) if equity_peak > 0 else 0.0
                if not brake_active and curr_dd >= self.drawdown_brake_pct:
                    brake_active = True
                elif brake_active and curr_dd <= self.drawdown_brake_pct * 0.5:
                    brake_active = False

            # 收集当日价格与 ATR%
            prices = {}
            atr_pct_map = {}
            data_slice = {}  # 传给策略的数据切片

            for ts_code, df in stock_data.items():
                idx = date_index[ts_code].get(date)
                if idx is not None:
                    prices[ts_code] = fast_data[ts_code].val("close", idx, 0.0)
                    atr_v = fast_data[ts_code].val("atr_pct", idx)
                    if atr_v is not None and atr_v > 0:
                        atr_pct_map[ts_code] = atr_v
                    # 零拷贝：列式视图语义等价于 df.iloc[:idx+1]，但列访问不再构造 Series
                    if use_fast_view:
                        data_slice[ts_code] = BarFrame(frame_stores[ts_code], 0, idx + 1)
                    else:
                        data_slice[ts_code] = df.iloc[:idx + 1]

            if not prices:
                continue

            # next_open 模式：次日开盘时撮合前一交易日生成的 pending_signals
            deferred_sells = list(pending_deferred_sells)
            pending_deferred_sells = []
            if self.execution_mode == "next_open" and pending_signals:
                # 横截面排序：卖出优先，买入按 score 降序
                pending_signals.sort(key=lambda s: (0 if s.direction == "SELL" else 1, -s.score))
                for sig in pending_signals:
                    ts_code = sig.ts_code
                    curr_idx = date_index.get(ts_code, {}).get(date)
                    if ts_code not in stock_data or curr_idx is None:
                        if sig.direction == "SELL":
                            deferred_sells.append(sig)
                        continue
                    df_stock = stock_data[ts_code]
                    fa = fast_data[ts_code]
                    open_price = fa.val("open", curr_idx, 0.0)
                    prev_close = fa.val("close", curr_idx - 1, 0.0)
                    bar_low = fa.val("low", curr_idx)
                    bar_high = fa.val("high", curr_idx)
                    decision_idx = curr_idx - 1 if curr_idx and curr_idx > 0 else None
                    ref_volume, ref_volatility = _liquidity_context(fa, decision_idx)
                    is_st, is_cy, is_bj = fa.is_st, fa.is_cy, fa.is_bj
                    snapshot = self._build_snapshot(sig, df_stock, curr_idx, fast=fa)

                    if sig.direction == "BUY":
                        if self.router_schedule and ts_code not in active_code_group:
                            continue
                        # 路由或单策略大盘择时二次确认
                        sig_allow_bear = single_allow_bear
                        if router_groups and ts_code in code_strategy_key:
                            s_inst = router_groups[code_strategy_key[ts_code]][0]
                            sig_allow_bear = bool(getattr(s_inst, "allow_bear_buy", False))
                        if not self._is_buy_regime_allowed(date, ts_code, sig_allow_bear, fa=fa, decision_idx=decision_idx):
                            continue

                        # 跳空高开过滤（与实盘推送一致：高开超过 max_open_gap_pct 不追高）
                        if self.max_open_gap_pct > 0 and prev_close > 0 and open_price > 0:
                            gap_pct = (open_price / prev_close - 1.0) * 100.0
                            if gap_pct > self.max_open_gap_pct:
                                continue

                        # 组合持仓上限 / 行业上限 / 相关性防抱团检查
                        cutoff_dt = fa.dates[decision_idx] if decision_idx is not None else date
                        if not self._can_open_position(ts_code, portfolio, cutoff_dt, fast_data):
                            continue

                        if open_price <= 0:
                            continue
                        budget = self._calc_buy_budget(
                            portfolio, sig, df_stock=df_stock,
                            curr_idx=decision_idx, fast=fa)
                        if brake_active:
                            budget *= 0.5
                        if self._is_bear_half_scale(date, ts_code, fa=fa, decision_idx=decision_idx):
                            budget *= self.bear_position_scale
                        volume = cap_order_volume(
                            int(budget / open_price) // 100 * 100, ref_volume)
                        if volume > 0:
                            bought = portfolio.buy(
                                ts_code=ts_code,
                                price=open_price,
                                volume=volume,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                                bar_low=bar_low,
                                bar_high=bar_high,
                                bar_volume=ref_volume,
                                volatility=ref_volatility,
                            )
                            if bought is not None and ts_code in active_code_group:
                                position_owner_group[ts_code] = active_code_group[ts_code]
                    elif sig.direction == "SELL":
                        pos = portfolio.get_position(ts_code)
                        if pos and not pos.is_empty:
                            avail_before = pos.available_shares
                            desired_sell, sell_vol = _resolve_sell_volume(avail_before, sig, ref_volume)
                            sold_trade = portfolio.sell(
                                ts_code=ts_code,
                                price=open_price,
                                volume=sell_vol,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                                bar_low=bar_low,
                                bar_high=bar_high,
                                bar_volume=ref_volume,
                                volatility=ref_volatility,
                            )
                            if pos.is_empty:
                                position_owner_group.pop(ts_code, None)
                            # 一字跌停拒单，或参与率上限导致部分成交时，顺延剩余份额至下一交易日继续卖出
                            if sell_vol > 0 and (
                                sold_trade is None
                                or (sell_vol < desired_sell and pos.available_shares > 0)
                            ):
                                deferred_sells.append(sig)
                pending_signals = []

            # 策略产生信号（支持路由分组 + 跨周期残留持仓按原属策略自然走完出场）
            signals = []
            if schedule_entries or router_groups:
                for _s_name, (s_inst, codes) in router_groups.items():
                    sub_slice = {c: data_slice[c] for c in codes if c in data_slice}
                    if not sub_slice:
                        continue
                    sub_sigs = s_inst.on_bar(date, sub_slice, portfolio)
                    if not sub_sigs:
                        continue
                    s_allow_bear = bool(getattr(s_inst, "allow_bear_buy", False))
                    for sig in sub_sigs:
                        if sig.direction == "BUY":
                            fa_s = fast_data.get(sig.ts_code)
                            s_idx = date_index.get(sig.ts_code, {}).get(date)
                            if not self._is_buy_regime_allowed(date, sig.ts_code, s_allow_bear, fa=fa_s, decision_idx=s_idx):
                                continue
                        signals.append(sig)

                # 对已被新路由剔除或改派但尚有未平仓的旧持仓：调用建仓期策略仅检查 SELL 出场，绝不强平
                for h_code, pos_obj in portfolio.positions.items():
                    if pos_obj.is_empty or h_code not in data_slice:
                        continue
                    owner_pair = position_owner_group.get(h_code)
                    if not owner_pair:
                        continue
                    owner_gk, _owner_bn = owner_pair
                    if owner_gk != active_code_group.get(h_code, (None, None))[0] and owner_gk in sched_strat_pool:
                        o_inst = sched_strat_pool[owner_gk][0]
                        o_sigs = o_inst.on_bar(date, {h_code: data_slice[h_code]}, portfolio)
                        for sig in (o_sigs or []):
                            if sig.direction == "SELL":
                                signals.append(sig)
            elif strategy is not None:
                raw_sigs = strategy.on_bar(date, data_slice, portfolio)
                for sig in raw_sigs:
                    if sig.direction == "BUY":
                        fa_s = fast_data.get(sig.ts_code)
                        s_idx = date_index.get(sig.ts_code, {}).get(date)
                        if not self._is_buy_regime_allowed(date, sig.ts_code, single_allow_bear, fa=fa_s, decision_idx=s_idx):
                            continue
                    signals.append(sig)

            # 风控拦截器检查（单策略风控 或 按持仓原属/当前路由分桶，保证跨年风控状态零丢失）
            risk_signals = []
            if sched_rm_pool:
                bucket_codes: dict = {}
                for h_code, pos_obj in portfolio.positions.items():
                    if pos_obj.is_empty:
                        continue
                    owner_pair = position_owner_group.get(h_code) or active_code_group.get(h_code)
                    b_n = owner_pair[1] if owner_pair else None
                    if b_n and b_n in sched_rm_pool:
                        bucket_codes.setdefault(b_n, set()).add(h_code)
                for b_n, codes_set in bucket_codes.items():
                    rm_inst = sched_rm_pool[b_n]
                    sub_view = _SubPortfolioView(portfolio, codes_set)
                    sub_prices = {c: prices[c] for c in codes_set if c in prices}
                    sub_atr = {c: atr_pct_map[c] for c in codes_set if c in atr_pct_map}
                    r_sigs = rm_inst.check_risks(date, sub_view, sub_prices, current_atr_pct=sub_atr)
                    if r_sigs:
                        risk_signals.extend(r_sigs)
            elif auto_rm_groups:
                for _s_name, (rm_inst, allowed_codes) in auto_rm_groups.items():
                    sub_view = _SubPortfolioView(portfolio, allowed_codes)
                    sub_prices = {c: prices[c] for c in allowed_codes if c in prices}
                    sub_atr = {c: atr_pct_map[c] for c in allowed_codes if c in atr_pct_map}
                    r_sigs = rm_inst.check_risks(date, sub_view, sub_prices, current_atr_pct=sub_atr)
                    if r_sigs:
                        risk_signals.extend(r_sigs)
            elif active_risk_manager is not None:
                try:
                    risk_signals = active_risk_manager.check_risks(
                        date, portfolio, prices, current_atr_pct=atr_pct_map
                    )
                except TypeError:
                    risk_signals = active_risk_manager.check_risks(date, portfolio, prices)

            if risk_signals:
                risk_sell_codes = {s.ts_code for s in risk_signals}
                filtered_signals = [s for s in signals if s.ts_code not in risk_sell_codes]
                signals = risk_signals + filtered_signals

            # 合并前日跌停未成交顺延的卖出单
            if deferred_sells:
                existing_sell_codes = {s.ts_code for s in signals if s.direction == "SELL"}
                for ds in deferred_sells:
                    if ds.ts_code not in existing_sell_codes:
                        signals = [ds] + [s for s in signals if s.ts_code != ds.ts_code]
                        existing_sell_codes.add(ds.ts_code)

            # 横截面优先级排序：卖出优先（释放资金），买入按 score 降序排列
            signals.sort(key=lambda s: (0 if s.direction == "SELL" else 1, -s.score))

            if self.execution_mode == "next_open":
                pending_signals = signals
            else:
                # current_close 模式：当日收盘价撮合
                for sig in signals:
                    ts_code = sig.ts_code
                    df_stock = stock_data.get(ts_code)
                    curr_idx = date_index.get(ts_code, {}).get(date)
                    # 当日停牌（无当日行情索引）绝不允许成交：顺延卖单按昨日信号价成交会制造虚假流动性
                    if df_stock is None or curr_idx is None:
                        if sig.direction == "SELL":
                            pending_deferred_sells.append(sig)
                        continue
                    fa = fast_data.get(ts_code)
                    prev_close = fa.val("close", curr_idx - 1, 0.0) if (fa is not None and curr_idx is not None) else 0.0
                    # 收盘撮合时当日 low/high 已经确定，可安全用于封板判定
                    bar_low = fa.val("low", curr_idx) if (fa is not None and curr_idx is not None) else None
                    bar_high = fa.val("high", curr_idx) if (fa is not None and curr_idx is not None) else None
                    is_st = fa.is_st if fa is not None else self.st_lookup.get(ts_code, False)
                    is_cy = fa.is_cy if fa is not None else ts_code.startswith(("300", "301", "688"))
                    is_bj = fa.is_bj if fa is not None else (
                        ts_code.startswith(("43", "83", "87", "92")) or ts_code.endswith(".BJ"))
                    # 收盘撮合时当日成交量已确定，可用于流动性约束
                    ref_volume, ref_volatility = _liquidity_context(fa, curr_idx)
                    snapshot = self._build_snapshot(sig, df_stock, curr_idx, fast=fa)

                    if sig.direction == "BUY":
                        if self.router_schedule and ts_code not in active_code_group:
                            continue
                        sig_allow_bear = single_allow_bear
                        if router_groups and ts_code in code_strategy_key:
                            s_inst = router_groups[code_strategy_key[ts_code]][0]
                            sig_allow_bear = bool(getattr(s_inst, "allow_bear_buy", False))
                        if not self._is_buy_regime_allowed(date, ts_code, sig_allow_bear, fa=fa, decision_idx=curr_idx):
                            continue

                        cutoff_dt = fa.dates[curr_idx - 1] if (fa is not None and curr_idx > 0) else date
                        if not self._can_open_position(ts_code, portfolio, cutoff_dt, fast_data):
                            continue

                        if sig.price_ref <= 0:
                            continue
                        budget = self._calc_buy_budget(portfolio, sig, df_stock=df_stock, curr_idx=curr_idx, fast=fa)
                        if brake_active:
                            budget *= 0.5
                        if self._is_bear_half_scale(date, ts_code, fa=fa, decision_idx=curr_idx):
                            budget *= self.bear_position_scale
                        volume = cap_order_volume(
                            int(budget / sig.price_ref) // 100 * 100, ref_volume)
                        if volume > 0:
                            bought = portfolio.buy(
                                ts_code=sig.ts_code,
                                price=sig.price_ref,
                                volume=volume,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                                bar_low=bar_low,
                                bar_high=bar_high,
                                bar_volume=ref_volume,
                                volatility=ref_volatility,
                            )
                            if bought is not None and sig.ts_code in active_code_group:
                                position_owner_group[sig.ts_code] = active_code_group[sig.ts_code]
                    elif sig.direction == "SELL":
                        pos = portfolio.get_position(sig.ts_code)
                        if pos and not pos.is_empty:
                            avail_before = pos.available_shares
                            desired_sell, sell_vol = _resolve_sell_volume(avail_before, sig, ref_volume)
                            sold_trade = portfolio.sell(
                                ts_code=sig.ts_code,
                                price=sig.price_ref,
                                volume=sell_vol,
                                trade_date=date,
                                prev_close=prev_close,
                                is_st=is_st,
                                is_cy=is_cy,
                                is_bj=is_bj,
                                context_snapshot=snapshot,
                                bar_low=bar_low,
                                bar_high=bar_high,
                                bar_volume=ref_volume,
                                volatility=ref_volatility,
                            )
                            if pos.is_empty:
                                position_owner_group.pop(sig.ts_code, None)
                            # 一字跌停拒单，或参与率上限导致部分成交时，顺延剩余份额到下一交易日重新挂单
                            if sell_vol > 0 and (
                                sold_trade is None
                                or (sell_vol < desired_sell and pos.available_shares > 0)
                            ):
                                pending_deferred_sells.append(sig)

            # 计提可用现金日无风险收益并记录当日权益
            portfolio.accrue_cash_yield(self.cash_yield_annual)
            portfolio.record_equity(date, prices)

        # 5. 计算指标（自动关联沪深300/上证指数基准计算 Alpha 与 Beta）
        #    基准收益按 trade_date 建字典传给绩效模块做精确日期 join，
        #    避免基准与组合交易日集合不一致时尾部截断造成的错位。
        #    get_benchmark_returns 带 LRU 缓存：网格搜索/样本外验证反复跑同一区间时不再重复查询。
        metrics = portfolio.calc_metrics(
            benchmark_returns=get_benchmark_returns(self.start_date, self.end_date) or None
        )

        result = BacktestResult(
            strategy=strat_label,
            params=json.dumps(self.params, ensure_ascii=False),
            start_date=self.start_date,
            end_date=self.end_date,
            initial_capital=self.initial_capital,
            final_capital=metrics.get("final_capital", self.initial_capital),
            total_return=metrics.get("total_return", 0),
            annual_return=metrics.get("annual_return", 0),
            max_drawdown=metrics.get("max_drawdown", 0),
            sharpe_ratio=metrics.get("sharpe_ratio", 0),
            calmar_ratio=metrics.get("calmar_ratio", 0),
            win_rate=metrics.get("win_rate", 0),
            trade_count=metrics.get("trade_count", 0),
            sell_count=metrics.get("sell_count", 0),
            equity_curve=portfolio.equity_curve,
            trades=portfolio.trades,
        )
        result.alpha = metrics.get("alpha", 0.0)
        result.beta = metrics.get("beta", 0.0)
        result.profit_factor = metrics.get("profit_factor", 1.0)
        result.win_loss_ratio = metrics.get("win_loss_ratio", 1.0)
        result.avg_win = metrics.get("avg_win", 0.0)
        result.avg_loss = metrics.get("avg_loss", 0.0)
        result.break_even_count = metrics.get("break_even_count", 0)

        # 6. 保存到数据库
        if save and portfolio.trades:
            bt_id = save_backtest_result(result)
            save_backtest_trades(bt_id, portfolio.trades)
            logger.info(f"回测结果已保存, ID={bt_id}")

        logger.info(
            f"回测完成: {strat_label} | "
            f"总收益 {result.total_return:.2f}% | "
            f"年化 {result.annual_return:.2f}% | "
            f"最大回撤 {result.max_drawdown:.2f}% | "
            f"夏普 {result.sharpe_ratio:.2f} | "
            f"交易 {result.trade_count} 次"
        )

        return result

    def _build_snapshot(self, sig, df_stock, curr_idx, fast=None) -> dict:
        """构建交易决策与风控执行的上下文快照

        Args:
            fast: 可选的 _StockArrays 列式视图。传入时按整数下标直接取标量，
                  避免在高维宽表上构造整行 Series（原本每次撮合都构造一次）。
        """
        snap = {
            "strategy": getattr(sig, "strategy", ""),
            "score": getattr(sig, "score", 0.0),
            "reason": getattr(sig, "reason", ""),
            "execution_mode": self.execution_mode,
        }
        if hasattr(sig, "context_snapshot") and sig.context_snapshot:
            snap.update(sig.context_snapshot)
        if df_stock is not None and curr_idx is not None and curr_idx < len(df_stock):
            if fast is not None:
                for col in _StockArrays.FAST_COLS:
                    v = fast.val(col, curr_idx)
                    if v is not None:
                        snap[col] = round(v, 2)
            else:
                row = df_stock.iloc[curr_idx]
                for col in ["close", "open", "high", "low", "ma5", "ma20", "ma60",
                            "rsi14", "dif", "dea", "kdj_k", "atr14"]:
                    if col in row and pd.notna(row[col]):
                        snap[col] = round(float(row[col]), 2)
        return snap

    def _calc_buy_budget(self, portfolio, sig, df_stock=None, curr_idx=None, fast=None) -> float:
        """根据组合总资产与持仓限制计算单笔买入预算（支持评分阶梯定仓与 ATR 波动率风险平价定仓）"""
        nav = portfolio.total_value
        max_pct = (1.0 / self.max_active_positions) if self.max_active_positions > 0 else 1.0

        if self.position_sizing == "atr_risk" and df_stock is not None and curr_idx is not None and curr_idx < len(df_stock):
            if fast is not None:
                atr_val = fast.val("atr14", curr_idx)
                ref_price = fast.val("close", curr_idx, 0.0)
            else:
                row = df_stock.iloc[curr_idx]
                atr_val = row.get("atr14")
                ref_price = float(row.get("close", sig.price_ref or 0.0))
            if atr_val is not None and pd.notna(atr_val) and float(atr_val) > 0 and ref_price > 0:
                atr_pct_val = (float(atr_val) / ref_price) * 100.0
                # 对齐策略真实 RiskManager 止损距离（不再硬编码 2*ATR 导致 KDJ/布林低估风险敞口）
                strat_key = str(getattr(sig, "strategy", "") or "").strip()
                mult, min_sl, max_sl = 2.0, 2.0, 20.0
                rm_match = None
                rm_pool = getattr(self, "sched_rm_pool", None) or {}
                if strat_key and strat_key in rm_pool:
                    rm_match = rm_pool[strat_key]
                elif self.risk_manager is not None and not isinstance(self.risk_manager, str):
                    rm_match = self.risk_manager
                if rm_match is not None and getattr(rm_match, "use_atr_stop", False):
                    mult = float(getattr(rm_match, "atr_stop_mult", 2.0))
                    min_sl = float(getattr(rm_match, "atr_min_sl_pct", 2.0))
                    max_sl = float(getattr(rm_match, "atr_max_sl_pct", 20.0))
                else:
                    try:
                        from engine.risk_manager import RiskManager
                        if strat_key in RiskManager.STRATEGY_PRESETS:
                            preset = RiskManager.STRATEGY_PRESETS[strat_key]
                            mult = float(preset.get("atr_stop_mult", 2.5))
                            min_sl = float(preset.get("atr_min_sl_pct", 3.5))
                            max_sl = float(preset.get("atr_max_sl_pct", 11.0))
                    except Exception:
                        pass
                eff_sl_pct = max(min_sl, min(max_sl, atr_pct_val * mult))
                stop_dist_pct = max(eff_sl_pct / 100.0, 0.02)
                # 风险平价仓位比例 = 单笔最大风险比例 / 真实策略止损距离比例，结合评分微调
                score_factor = 0.8 + 0.4 * max(0.0, min(float(getattr(sig, "score", 0.7)), 1.0))
                atr_pct = (self.risk_per_trade / stop_dist_pct) * score_factor
                cap_pct = min(max_pct, 0.40) if self.max_active_positions != 1 else 1.0
                target_pct = max(0.05, min(atr_pct, cap_pct))
                ideal_budget = nav * target_pct
                return max(0.0, min(portfolio.cash, ideal_budget))

        if self.max_active_positions > 0:
            target_pct = min(max_pct, 1.0)
        else:
            target_pct = min(BACKTEST_MIN_POSITION_PCT + sig.score * BACKTEST_POSITION_STEP, 1.0)
        # 以组合当前总净值 (NAV) 定额，并受限于可用现金
        ideal_budget = nav * target_pct
        return max(0.0, min(portfolio.cash, ideal_budget))


@lru_cache(maxsize=BACKTEST_BENCHMARK_CACHE_SIZE)
def _preload_cached(universe: tuple, start_date: str, end_date: str) -> dict:
    data = {}
    warmup_start = ""
    if start_date:
        try:
            from datetime import datetime, timedelta
            dt = datetime.strptime(start_date, "%Y%m%d")
            warmup_start = (dt - timedelta(days=180)).strftime("%Y%m%d")
        except Exception:
            warmup_start = start_date

    for ts_code in universe:
        df = get_daily(ts_code, warmup_start, end_date)
        if df.empty:
            continue
        df = clean_daily(df)
        if df.empty:
            continue
        df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "vol_ma", "kdj", "atr"])
        data[ts_code] = df
    return data


def preload_backtest_data(universe: list, start_date: str, end_date: str,
                          use_cache: bool = True) -> dict:
    """预加载行情并预计算常用指标，用于多组回测与网格搜索共享内存（消除重复 I/O）

    同一 (标的集合, 起止日期) 会命中进程级缓存。样本外验证会在多个窗口上
    反复调用本函数，缓存可避免重复拉取与重算指标。
    """
    if not use_cache:
        return _preload_cached.__wrapped__(tuple(universe), start_date, end_date)
    return _preload_cached(tuple(universe), start_date, end_date)


def clear_preload_cache():
    """清空预加载数据缓存（释放内存 / 测试隔离）"""
    _preload_cached.cache_clear()


# 并行网格搜索的工作进程内共享数据。通过 initializer 注入一次，
# 避免把整个 preloaded 字典随每个任务 pickle 一遍（N 个组合 = N 份数据）。
_WORKER_STATE: dict = {}


def _init_grid_worker(preloaded_data: dict, backtest_options: dict = None):
    _WORKER_STATE["preloaded"] = preloaded_data
    _WORKER_STATE["options"] = backtest_options or {}


def _execute_single_grid_combo(args):
    """顶层并行网格搜索执行函数（支持 Windows 跨进程序列化）

    支持两种入参：
      - 9 元组（旧接口，携带 preloaded_data，向后兼容）
      - (params, strategy_cls, start_date, end_date, initial_capital,
         execution_mode, risk_manager, universe) —— 工作进程从 initializer
        注入的全局状态取预加载数据，不再逐任务传输。
    """
    if len(args) == 8 and isinstance(args[0], dict):
        params, strategy_cls, start_date, end_date, initial_capital, execution_mode, risk_manager, universe = args
        # 未经 initializer 直接调用（如单元测试）时没有共享数据，退回自行加载
        preloaded_data = _WORKER_STATE.get("preloaded")
    else:
        if len(args) == 7:
            strategy_cls, params, universe, start_date, end_date, initial_capital, preloaded_data = args
            execution_mode, risk_manager = "next_open", None
        else:
            strategy_cls, params, universe, start_date, end_date, initial_capital, preloaded_data, execution_mode, risk_manager = args
    from engine.risk_manager import clone_risk_manager
    bt = Backtester(
        strategy_cls=strategy_cls,
        params=params,
        universe=universe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        execution_mode=execution_mode,
        # 并行 worker 内多个组合可能复用同一进程，必须逐组合克隆风控状态
        risk_manager=clone_risk_manager(risk_manager),
        preloaded_data=preloaded_data,
        **_options_kwargs(_WORKER_STATE.get("options")),
    )
    return bt.run(save=False)


def grid_search(strategy_cls, universe: list, start_date: str, end_date: str,
                initial_capital: float = 100000, param_grid: dict = None,
                metric: str = "total_return", progress_callback: Callable = None,
                execution_mode: str = "next_open", risk_manager = None,
                preloaded_data: dict = None,
                backtest_options: dict = None) -> list:
    """参数网格搜索（带数据预热加速，默认对齐 next_open 实盘撮合防过拟合）

    遍历所有参数组合，运行回测并返回按指定指标排序的结果。

    Args:
        preloaded_data: 可选的预加载数据。样本外验证会在多个窗口间复用同一份预加载
            结果，避免每个折、每个参数组合都重复拉取行情与重算指标。
        backtest_options: 由 ``build_backtest_options()`` 生成的引擎配置。选参阶段
            必须使用与单次回测相同的仓位/持仓/择时规则，否则结果不可比。
    """
    if param_grid is None:
        param_grid = {}

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    # Tier 1 优化：在网格循环前单次预热加载数据与指标计算
    preloaded = preloaded_data if preloaded_data is not None else preload_backtest_data(
        universe, start_date, end_date)

    results = []
    for i, combo in enumerate(combinations):
        params = dict(zip(param_names, combo))

        # 每轮回测克隆独立风控实例：既隔离 highest_prices 状态，也完整保留
        # 保本止损等全部配置（旧的手工复制会静默丢掉 3 个字段）
        from engine.risk_manager import clone_risk_manager
        rm_copy = clone_risk_manager(risk_manager)

        bt = Backtester(
            strategy_cls=strategy_cls,
            params=params,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            execution_mode=execution_mode,
            risk_manager=rm_copy,
            preloaded_data=preloaded,
            **_options_kwargs(backtest_options),
        )
        result = bt.run(save=False)

        metric_value = getattr(result, metric, 0)
        if metric == "max_drawdown":
            metric_value = -metric_value

        results.append({
            "params": params,
            "result": result,
            "metric_value": metric_value,
        })

        if progress_callback:
            progress_callback(i + 1, total)

    results.sort(key=lambda r: r["metric_value"], reverse=True)
    return results


def grid_search_parallel(strategy_cls, universe: list, start_date: str, end_date: str,
                          initial_capital: float = 100000, param_grid: dict = None,
                          metric: str = "total_return", max_workers: int = None,
                          progress_callback: Callable = None,
                          execution_mode: str = "next_open", risk_manager = None,
                          backtest_options: dict = None) -> list:
    """并行参数网格搜索（基于 ProcessPoolExecutor + 数据预热加速）

    适用于参数组合较多（>10）的场景。利用多核 CPU 并行执行，并共享预加载数据。
    """
    if param_grid is None:
        param_grid = {}

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)

    if total == 0:
        return []

    # Tier 1 优化：主进程一次性预处理，子任务零重复 I/O
    preloaded = preload_backtest_data(universe, start_date, end_date)

    tasks = []
    for combo in combinations:
        params = dict(zip(param_names, combo))
        # 只传参数，预加载数据通过 initializer 一次性注入各工作进程，
        # 避免 N 个任务各自 pickle 一份完整行情字典（全市场扫描时会直接爆内存）
        args = (params, strategy_cls, start_date, end_date, initial_capital,
                execution_mode, risk_manager, universe)
        tasks.append((combo, args))

    results = []
    completed = 0
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=_init_grid_worker,
            initargs=(preloaded, backtest_options)) as executor:
        futures = {executor.submit(_execute_single_grid_combo, args): combo
                   for combo, args in tasks}

        for future in concurrent.futures.as_completed(futures):
            combo = futures[future]
            try:
                result = future.result()
                params = dict(zip(param_names, combo))
                metric_value = getattr(result, metric, 0)
                if metric == "max_drawdown":
                    metric_value = -metric_value

                results.append({
                    "params": params,
                    "result": result,
                    "metric_value": metric_value,
                })
            except Exception as e:
                logger.error(f"并行回测异常 ({combo}): {e}")

            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    results.sort(key=lambda r: r["metric_value"], reverse=True)
    return results


def _add_calendar_months(dt, months: int):
    """纯标准库按自然月增减日期，月末自动对齐。"""
    import calendar
    total_m = dt.year * 12 + (dt.month - 1) + int(months)
    year = total_m // 12
    month = total_m % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def run_rolling_adaptive_backtest(
    universe: list,
    start_date: str,
    end_date: str,
    rebalance_months: int = 12,
    train_lookback_years: int = 3,
    initial_capital: float = 100000.0,
    progress_callback: Callable = None,
    **bt_kwargs,
) -> dict:
    """严格零前视滚动自适应组合回测：每期只用严格早于样本外起点的历史窗口定策略路由，冻结跑下一区间并复利拼接。"""
    from datetime import datetime, timedelta
    from data.storage import get_instrument_list
    from services.strategy_recommender import (
        evaluate_stock_strategy, is_no_trade_recommendation,
    )
    from strategies import STRATEGY_REGISTRY

    clean_universe = list(dict.fromkeys(str(c).strip() for c in (universe or []) if str(c).strip()))
    start_s = str(start_date).replace("-", "").replace("/", "").strip()[:8]
    end_s = str(end_date).replace("-", "").replace("/", "").strip()[:8]
    dt_s = datetime.strptime(start_s, "%Y%m%d")
    dt_e = datetime.strptime(end_s, "%Y%m%d")
    rb_months = max(3, int(rebalance_months or 12))
    lb_years = max(1, int(train_lookback_years or 2))

    # 1. 切分连续不重叠的样本外子期
    segments = []
    cur = dt_s
    while cur <= dt_e:
        nxt = _add_calendar_months(cur, rb_months)
        seg_e = min(dt_e, nxt - timedelta(days=1))
        if segments and (seg_e - cur).days < 15:
            prev_s, _ = segments[-1]
            segments[-1] = (prev_s, seg_e)
        else:
            segments.append((cur, seg_e))
        if seg_e >= dt_e:
            break
        cur = nxt

    if not segments or not clean_universe:
        empty_res = BacktestResult(
            strategy="rolling_adaptive_router", params="{}",
            start_date=start_s, end_date=end_s,
            initial_capital=float(initial_capital), final_capital=float(initial_capital),
        )
        return {"result": empty_res, "periods": []}

    # 2. 一次性预加载覆盖最长回看训练窗口 + 全部测试区间的行情
    preload_start = (_add_calendar_months(dt_s, -12 * lb_years) - timedelta(days=180)).strftime("%Y%m%d")
    preloaded = preload_backtest_data(clean_universe, preload_start, end_s)

    try:
        inst_df = get_instrument_list()
        inst_map = (
            inst_df.set_index("ts_code")[["name", "industry"]].to_dict("index")
            if (inst_df is not None and not inst_df.empty and "industry" in inst_df.columns)
            else {}
        )
    except Exception:
        inst_map = {}

    # 提取测试期各交易日全集（用于空仓现金防守期对齐净值日历）
    all_trade_dates = sorted({
        str(d)
        for df in preloaded.values()
        if df is not None and not df.empty and "trade_date" in df.columns
        for d in df["trade_date"].astype(str).tolist()
        if start_s <= str(d) <= end_s
    })

    run_bt_kwargs = dict(bt_kwargs)
    run_bt_kwargs.setdefault("execution_mode", "next_open")
    run_bt_kwargs.setdefault("risk_manager", "auto")
    run_bt_kwargs.setdefault("max_active_positions", 5)
    run_bt_kwargs.setdefault("position_sizing", "atr_risk")
    run_bt_kwargs.setdefault("bear_position_scale", 0.5)
    run_bt_kwargs.setdefault("market_regime_filter", True)
    # 避免外层重复透传与固定参数冲突
    for k_drop in ("strategy_cls", "params", "universe", "start_date", "end_date",
                   "initial_capital", "preloaded_data", "strategy_router", "router_schedule"):
        run_bt_kwargs.pop(k_drop, None)

    router_schedule: dict = {}
    period_meta: list = []
    total_segs = len(segments)
    for idx_s, (seg_s_dt, seg_e_dt) in enumerate(segments):
        seg_s = seg_s_dt.strftime("%Y%m%d")
        seg_e = seg_e_dt.strftime("%Y%m%d")
        tr_s = _add_calendar_months(seg_s_dt, -12 * lb_years).strftime("%Y%m%d")
        tr_e = (seg_s_dt - timedelta(days=1)).strftime("%Y%m%d")

        router = {}
        blocked_codes = []
        strat_counts: dict = {}
        for code in clean_universe:
            meta = inst_map.get(code, {})
            try:
                ev = evaluate_stock_strategy(
                    code, name=meta.get("name", ""), industry=meta.get("industry", ""),
                    start_date=tr_s, end_date=tr_e, preloaded_data=preloaded,
                )
            except TypeError:
                ev = evaluate_stock_strategy(
                    code, name=meta.get("name", ""), industry=meta.get("industry", ""),
                    start_date=tr_s, end_date=tr_e,
                )
            if is_no_trade_recommendation(ev.get("tag", "")) or (ev.get("is_oos") and ev.get("oos_passed") is False):
                blocked_codes.append(code)
                continue
            sk = ev.get("strategy", "kdj_cross")
            if sk in STRATEGY_REGISTRY and not getattr(STRATEGY_REGISTRY[sk], "is_cross_sectional", False):
                s_cls = STRATEGY_REGISTRY[sk]
                s_params = {k: v.get("default", 0) for k, v in s_cls.param_schema.items()}
                router[code] = (s_cls, s_params)
                strat_counts[sk] = strat_counts.get(sk, 0) + 1
            else:
                blocked_codes.append(code)

        router_schedule[seg_s] = router
        dist_txt = ", ".join(f"{k}:{v}" for k, v in sorted(strat_counts.items())) if router else "空仓防御"
        period_meta.append((seg_s, seg_e, tr_s, tr_e, len(router), len(blocked_codes), dist_txt))
        if progress_callback:
            progress_callback(idx_s + 1, total_segs + 1)

    # 单次连续组合贯通全周期：持仓与风控跨期无缝衔接，彻底消除跨年零成本强平失真
    bt_cont = Backtester(
        strategy_cls=None,
        params={},
        universe=clean_universe,
        start_date=start_s,
        end_date=end_s,
        initial_capital=float(initial_capital),
        preloaded_data=preloaded,
        router_schedule=router_schedule,
        **run_bt_kwargs,
    )
    res = bt_cont.run(save=False)
    res.strategy = "rolling_adaptive_router"
    res.params = json.dumps({
        "rebalance_months": rb_months,
        "train_lookback_years": lb_years,
        "universe_size": len(clean_universe),
        "continuous_portfolio": True,
    }, ensure_ascii=False)

    period_rows = []
    prev_end_cap = float(initial_capital)
    full_eq = res.equity_curve or []
    full_trades = res.trades or []
    for seg_s, seg_e, tr_s, tr_e, n_act, n_blk, dist_txt in period_meta:
        seg_eq = [pt for pt in full_eq if seg_s <= str(pt.get("date", "")) <= seg_e]
        seg_tr = [t for t in full_trades if seg_s <= str(getattr(t, "trade_date", "")) <= seg_e]
        end_cap = float(seg_eq[-1]["equity"]) if seg_eq else prev_end_cap
        seg_port = Portfolio(prev_end_cap)
        seg_port.equity_curve = seg_eq
        seg_port.trades = seg_tr
        seg_m = seg_port.calc_metrics() if seg_eq else {}
        seg_ret = ((end_cap / prev_end_cap) - 1.0) * 100.0 if prev_end_cap > 0 else 0.0
        period_rows.append({
            "区间": f"{seg_s}~{seg_e}",
            "训练窗口": f"{tr_s}~{tr_e}",
            "入池标的": n_act,
            "观望熔断": n_blk,
            "路由分布": dist_txt,
            "区间收益%": round(seg_ret, 2),
            "最大回撤%": round(float(seg_m.get("max_drawdown", 0.0)), 2),
            "胜率%": round(float(seg_m.get("win_rate", 0.0)), 1),
            "盈亏比": round(float(seg_m.get("profit_factor", 1.0)), 2),
            "平仓笔数": int(seg_m.get("sell_count", 0)),
            "期末净值": round(end_cap, 2),
        })
        prev_end_cap = end_cap

    if progress_callback:
        progress_callback(total_segs + 1, total_segs + 1)

    return {"result": res, "periods": period_rows}


def run_dual_sleeve_backtest(
    stock_universe: list,
    etf_universe: list,
    start_date: str,
    end_date: str,
    stock_weight: float = 0.6,
    rebalance_months: int = 12,
    train_lookback_years: int = 3,
    etf_lookback: int = 20,
    etf_rebalance_days: int = 10,
    etf_top_k: int = 2,
    etf_position_sizing: str = "atr_risk",
    initial_capital: float = 100000.0,
    progress_callback: Callable = None,
    **bt_kwargs,
) -> dict:
    """双舱位核心-卫星组合回测（个股零前视滚动路由 + 多资产 ETF 动量轮动）。

    解决单股反转等待期长期 ~70% 现金闲置问题：按比例分配至双独立子账户并按真实交易日历合并净值与成交归因。
    """
    from strategies.momentum_rotation import MomentumRotationStrategy

    w_stock = min(0.9, max(0.1, float(stock_weight)))
    w_etf = round(1.0 - w_stock, 6)
    cap_total = float(initial_capital)
    cap_stock = round(cap_total * w_stock, 2)
    cap_etf = round(cap_total - cap_stock, 2)

    start_s = str(start_date).replace("-", "").replace("/", "").strip()[:8]
    end_s = str(end_date).replace("-", "").replace("/", "").strip()[:8]
    clean_stocks = list(dict.fromkeys(str(c).strip() for c in (stock_universe or []) if str(c).strip()))
    clean_etfs = list(dict.fromkeys(str(c).strip() for c in (etf_universe or []) if str(c).strip()))

    def _on_stock_prog(done_s, tot_s):
        if progress_callback:
            progress_callback(done_s, max(1, tot_s) + 1)

    stock_out = run_rolling_adaptive_backtest(
        universe=clean_stocks,
        start_date=start_s,
        end_date=end_s,
        rebalance_months=int(rebalance_months),
        train_lookback_years=int(train_lookback_years),
        initial_capital=cap_stock,
        progress_callback=_on_stock_prog,
        **bt_kwargs,
    )
    stock_res = stock_out["result"]
    stock_periods = stock_out["periods"]

    exec_mode = bt_kwargs.get("execution_mode", "next_open")
    cash_yield = float(bt_kwargs.get("cash_yield_annual", 0.0) or 0.0)
    etf_preloaded = bt_kwargs.get("etf_preloaded_data")
    etf_sz = "atr_risk" if str(etf_position_sizing or "atr_risk").strip() == "atr_risk" else "score"
    if clean_etfs:
        bt_etf = Backtester(
            strategy_cls=MomentumRotationStrategy,
            params={
                "lookback": int(etf_lookback),
                "rebalance_days": int(etf_rebalance_days),
                "top_k": int(etf_top_k),
                "ma_filter_period": 20,
            },
            universe=clean_etfs,
            start_date=start_s,
            end_date=end_s,
            initial_capital=cap_etf,
            execution_mode=exec_mode,
            risk_manager=None,
            max_active_positions=int(etf_top_k),
            position_sizing=etf_sz,
            market_regime_filter=False,
            preloaded_data=etf_preloaded,
            cash_yield_annual=cash_yield,
        )
        etf_res = bt_etf.run(save=False)
    else:
        etf_res = BacktestResult(
            strategy="momentum_rotation", params="{}",
            start_date=start_s, end_date=end_s,
            initial_capital=cap_etf, final_capital=cap_etf,
        )

    s_curve = {str(pt["date"]): pt for pt in (stock_res.equity_curve or []) if "date" in pt}
    e_curve = {str(pt["date"]): pt for pt in (etf_res.equity_curve or []) if "date" in pt}
    all_dates = sorted(set(s_curve.keys()) | set(e_curve.keys()))

    combined_curve = []
    last_s = {"equity": cap_stock, "cash": cap_stock}
    last_e = {"equity": cap_etf, "cash": cap_etf}
    for dt in all_dates:
        if dt in s_curve:
            last_s = s_curve[dt]
        if dt in e_curve:
            last_e = e_curve[dt]
        tot_eq = round(float(last_s.get("equity", 0.0)) + float(last_e.get("equity", 0.0)), 2)
        tot_cash = round(float(last_s.get("cash", 0.0)) + float(last_e.get("cash", 0.0)), 2)
        combined_curve.append({"date": dt, "equity": tot_eq, "cash": tot_cash})

    merged_trades = []
    for sleeve_label, tr_list in (("个股滚动", stock_res.trades or []), ("ETF轮动", etf_res.trades or [])):
        for t in tr_list:
            snap = dict(getattr(t, "context_snapshot", None) or {})
            snap.setdefault("sleeve", sleeve_label)
            if not snap.get("entry_strategy"):
                snap["entry_strategy"] = str(snap.get("strategy", "") or "")
            merged_trades.append(Trade(
                ts_code=t.ts_code,
                direction=t.direction,
                trade_date=t.trade_date,
                price=t.price,
                volume=t.volume,
                commission=t.commission,
                tax=t.tax,
                pnl=t.pnl,
                holding_days=t.holding_days,
                context_snapshot=snap,
            ))
    merged_trades.sort(key=lambda tr: (str(tr.trade_date), 0 if tr.direction == "SELL" else 1))

    pf = Portfolio(cap_total)
    pf.equity_curve = combined_curve
    pf.trades = merged_trades
    if combined_curve:
        pf.cash = float(combined_curve[-1]["cash"])
    metrics = pf.calc_metrics(
        benchmark_returns=get_benchmark_returns(start_s, end_s) or None
    ) if combined_curve else {}

    combined_res = BacktestResult(
        strategy="dual_sleeve_core_satellite",
        params=json.dumps({
            "stock_weight": round(w_stock, 2),
            "etf_weight": round(w_etf, 2),
            "rebalance_months": int(rebalance_months),
            "train_lookback_years": int(train_lookback_years),
            "etf_rebalance_days": int(etf_rebalance_days),
            "etf_top_k": int(etf_top_k),
            "etf_position_sizing": etf_sz,
        }, ensure_ascii=False),
        start_date=start_s,
        end_date=end_s,
        initial_capital=cap_total,
        final_capital=float(metrics.get("final_capital", cap_total)),
        total_return=float(metrics.get("total_return", 0.0)),
        annual_return=float(metrics.get("annual_return", 0.0)),
        max_drawdown=float(metrics.get("max_drawdown", 0.0)),
        sharpe_ratio=float(metrics.get("sharpe_ratio", 0.0)),
        calmar_ratio=float(metrics.get("calmar_ratio", 0.0)),
        win_rate=float(metrics.get("win_rate", 0.0)),
        trade_count=int(metrics.get("trade_count", len(merged_trades))),
        sell_count=int(metrics.get("sell_count", 0)),
        equity_curve=combined_curve,
        trades=merged_trades,
    )
    combined_res.alpha = float(metrics.get("alpha", 0.0))
    combined_res.beta = float(metrics.get("beta", 0.0))
    combined_res.profit_factor = float(metrics.get("profit_factor", 1.0))
    combined_res.win_loss_ratio = float(metrics.get("win_loss_ratio", 1.0))
    combined_res.avg_win = float(metrics.get("avg_win", 0.0))
    combined_res.avg_loss = float(metrics.get("avg_loss", 0.0))
    combined_res.break_even_count = int(metrics.get("break_even_count", 0))

    if progress_callback:
        progress_callback(1, 1)

    return {
        "result": combined_res,
        "stock_result": stock_res,
        "etf_result": etf_res,
        "periods": stock_periods,
        "weights": {"stock": w_stock, "etf": w_etf},
    }
