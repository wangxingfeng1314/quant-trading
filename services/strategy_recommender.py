"""智能策略推荐引擎 (Strategy Recommender Service)

功能:
1. 快速回测评估：基于标的近期历史行情数据，对4大冠军策略（短线KDJ、震荡布林、顺势双均线、综合多因子）进行真实撮合回测；
2. 收益与胜率综合决策：结合策略胜率、累计收益、最大回撤，自动为标的匹配最佳策略；
3. 启发式冷启动：当标的无历史行情时，根据行业属性与标的类型进行特征推荐；
4. 批量更新自选股推荐标签，供自选股列表与信号跟踪直观展示。
"""
import logging
from datetime import datetime
import pandas as pd

from data.storage import (
    get_daily, get_watchlist, get_instrument_list, update_watchlist_recommendation,
)
from engine.backtester import Backtester, preload_backtest_data
from engine.risk_manager import RiskManager
from strategies import STRATEGY_REGISTRY

logger = logging.getLogger(__name__)

PREFIX_MAP = {
    "kdj_cross": "⚡ KDJ短线企稳",
    "bollinger_reversal": "↔️ 布林反转",
    "ma_cross": "📈 双均线趋势",
    "signal_combo": "🧩 多因子共振",
}


def evaluate_stock_strategy(ts_code: str, name: str = "", industry: str = "",
                            start_date: str = "2023-01-01", end_date: str = "") -> dict:
    """评估单只股票/ETF最匹配的交易策略

    参数:
        ts_code: 标的代码 (如 600460.SH)
        name: 标的中文名称
        industry: 所属行业或板块
        start_date: 回测起始日期 (默认 2023-01-01)
        end_date: 回测截止日期 (空则为今日)

    返回:
        {
            "strategy": str,        # 策略键名 (如 'ma_cross')
            "strategy_cn": str,     # 中文简称 (如 '双均线趋势')
            "tag": str,             # 界面展示标签 (如 '📈 双均线趋势 (胜率55.6%/+8.9%)')
            "reason": str,          # 推荐原因详解
            "details": dict,        # 4大策略的回测对比数据
            "is_backtested": bool,  # 是否基于真实回测数据
        }
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    df = get_daily(ts_code)
    # 若数据不足60日线，采用行业特征启发式推荐
    if df.empty or len(df) < 60:
        feature_text = f"{name} {industry} {ts_code}".upper()
        is_growth = any(k in feature_text for k in [
            "半导体", "芯片", "电子", "科技", "军工", "新能", "光伏", "通信", "计算机", "软件", "AI"
        ])
        is_defensive = any(k in feature_text for k in [
            "电力", "煤炭", "银行", "保险", "公路", "港口", "红利", "ETF", "300", "50"
        ])

        if is_growth:
            strategy_key = "ma_cross"
            tag = "📈 双均线趋势 (成长弹性推荐)"
            reason = "该标的具有成长性与高弹性特征，推荐采用双均线顺势追踪捕捉主升浪趋势。"
        elif is_defensive:
            strategy_key = "bollinger_reversal"
            tag = "↔️ 布林反转 (高股息防御推荐)"
            reason = "该标的具有稳健慢牛与低波动特征，推荐采用布林带触轨高抛低吸。"
        else:
            strategy_key = "kdj_cross"
            tag = "⚡ KDJ短线企稳 (稳健底仓推荐)"
            reason = "标的暂无完整历史日线，默认匹配短线超卖企稳金叉策略。"

        return {
            "strategy": strategy_key,
            "strategy_cn": PREFIX_MAP.get(strategy_key, strategy_key),
            "tag": tag,
            "reason": reason,
            "details": {},
            "is_backtested": False,
        }

    # 具备历史日线：执行快速全策略比对
    try:
        preloaded = preload_backtest_data([ts_code], start_date, end_date)
    except Exception as e:
        logger.warning(f"预加载标的 {ts_code} 数据失败: {e}")
        preloaded = None

    stats = {}
    strategy_candidates = ["kdj_cross", "bollinger_reversal", "ma_cross", "signal_combo"]

    for s_name in strategy_candidates:
        if s_name not in STRATEGY_REGISTRY:
            continue
        s_cls = STRATEGY_REGISTRY[s_name]
        params = {k: v.get("default", 0) for k, v in s_cls.param_schema.items()}
        rm = RiskManager(stop_loss_pct=-6.0, trailing_stop_activation=12.0, trailing_stop_callback=3.0)
        try:
            bt = Backtester(
                strategy_cls=s_cls,
                params=params,
                universe=[ts_code],
                start_date=start_date,
                end_date=end_date,
                initial_capital=100000.0,
                execution_mode="next_open",
                risk_manager=rm,
                preloaded_data=preloaded,
            )
            res = bt.run(save=False)
            stats[s_name] = {
                "total_return": float(res.total_return),
                "win_rate": float(res.win_rate),
                "trades": int(res.trade_count),
                "sells": int(getattr(res, "sell_count", 0)),
                "mdd": float(res.max_drawdown),
                "sharpe": float(res.sharpe_ratio),
            }
        except Exception as e:
            logger.warning(f"标的 {ts_code} 回测策略 {s_name} 异常: {e}")
            stats[s_name] = {
                "total_return": 0.0, "win_rate": 0.0, "trades": 0, "sells": 0, "mdd": 0.0, "sharpe": 0.0
            }

    if not stats:
        return {
            "strategy": "kdj_cross",
            "strategy_cn": "⚡ KDJ短线企稳",
            "tag": "⚡ KDJ短线企稳",
            "reason": "策略回测无有效结果，采用默认推荐。",
            "details": {},
            "is_backtested": False,
        }

    # 综合效用评分：
    # 评分 = 收益*1.5 + (胜率-40)*0.6 - 最大回撤*0.4
    best_s = None
    best_score = -99999.0
    for s_name, m in stats.items():
        if m["trades"] == 0:
            score = -999.0
        else:
            score = m["total_return"] * 1.5 + (m["win_rate"] - 40.0) * 0.6 - m["mdd"] * 0.4
        if score > best_score:
            best_score = score
            best_s = s_name

    if best_s is None:
        best_s = "kdj_cross"

    best_m = stats[best_s]
    prefix = PREFIX_MAP.get(best_s, best_s)

    # 生成展示标签
    if best_m["win_rate"] >= 55.0:
        tag = f"{prefix} (胜率{best_m['win_rate']:.1f}%/{best_m['total_return']:+.1f}%)"
    else:
        tag = f"{prefix} ({best_m['total_return']:+.1f}%/回撤{best_m['mdd']:.1f}%)"

    reason = (
        f"在实盘回测中表现最佳：总收益 {best_m['total_return']:+.2f}%，"
        f"胜率 {best_m['win_rate']:.1f}%，最大回撤 {best_m['mdd']:.2f}%。"
    )

    return {
        "strategy": best_s,
        "strategy_cn": prefix,
        "tag": tag,
        "reason": reason,
        "details": stats,
        "is_backtested": True,
    }


def batch_update_watchlist_recommendations(force: bool = False,
                                           progress_callback = None) -> int:
    """批量更新所有自选股的推荐策略标签

    参数:
        force: 若为 True，则重新评估全部自选股；若为 False，仅评估尚无推荐标签的标的
        progress_callback: 进度回调 (current, total, ts_code, name)

    返回:
        更新成功的标的数量
    """
    wl = get_watchlist()
    if wl.empty:
        return 0

    inst_df = get_instrument_list()
    name_map = dict(zip(inst_df["ts_code"], inst_df["name"])) if not inst_df.empty else {}
    ind_map = dict(zip(inst_df["ts_code"], inst_df.get("industry", ""))) if not inst_df.empty and "industry" in inst_df.columns else {}

    targets = []
    for _, r in wl.iterrows():
        rec = r.get("recommended_strategy", "")
        if force or not rec or not str(rec).strip():
            targets.append(r["ts_code"])

    if not targets:
        return 0

    updated = 0
    total = len(targets)
    for idx, code in enumerate(targets):
        c_name = name_map.get(code, "")
        c_ind = ind_map.get(code, "")
        if progress_callback:
            progress_callback(idx + 1, total, code, c_name)

        res = evaluate_stock_strategy(code, name=c_name, industry=c_ind)
        update_watchlist_recommendation(code, res["tag"])
        updated += 1

    return updated
