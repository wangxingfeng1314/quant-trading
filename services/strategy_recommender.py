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

# 样本外切分参数：训练段选策略，测试段冻结策略后评估并展示业绩。
# 旧实现让 4 个策略在同一段全样本上取 argmax，还把这段「样本内最优」
# 的收益/胜率当作推荐依据展示，属于典型的选择偏差 + 多重比较偏差。
OOS_TRAIN_RATIO = 0.7
OOS_MIN_TEST_DAYS = 20
OOS_MIN_TRADES = 3


def _norm_date(value) -> str:
    """把 2023-01-01 / 20230101 / 2023/01/01 统一成 YYYYMMDD"""
    return str(value or "").replace("-", "").replace("/", "").strip()


def _split_oos_window(dates: list):
    """按交易日切出 (训练段日期, 测试段日期)；数据不足返回 None"""
    if len(dates) < OOS_MIN_TEST_DAYS + 20:
        return None
    cut = int(len(dates) * OOS_TRAIN_RATIO)
    cut = min(max(cut, 20), len(dates) - OOS_MIN_TEST_DAYS)
    if cut >= len(dates):
        return None
    return dates[:cut], dates[cut:]


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
            "details": dict,        # 4大策略在「训练段」的样本内对比数据
            "is_backtested": bool,  # 是否基于真实回测数据
            "is_oos": bool,         # 展示业绩是否来自样本外测试段
            "train_window": list,   # [训练段起, 训练段止]
            "test_window": list,    # [测试段起, 测试段止]
            "oos_total_return": float, "oos_win_rate": float, "oos_trades": int,
        }

    方法论：训练段只用于「选哪个策略」，测试段冻结该策略后单独回测，
    tag / reason 只引用测试段指标，杜绝「同一段数据既择优又展示业绩」。
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

    # ---------- 样本外切分：训练段选策略，测试段冻结策略后评估 ----------
    start = _norm_date(start_date)
    end = _norm_date(end_date)
    all_dates = sorted({
        _norm_date(d) for d in df["trade_date"].astype(str).tolist()
        if start <= _norm_date(d) <= end
    })
    split = _split_oos_window(all_dates)
    if split is None:
        return {
            "strategy": "kdj_cross",
            "strategy_cn": PREFIX_MAP["kdj_cross"],
            "tag": "⚡ KDJ短线企稳 (样本不足)",
            "reason": "区间内交易日不足以切出训练段/测试段，不展示业绩标签，"
                      "避免把样本内最优收益当作推荐依据。",
            "details": {},
            "is_backtested": False,
            "is_oos": False,
        }

    train_dates, test_dates = split
    train_start, train_end = train_dates[0], train_dates[-1]
    test_start, test_end = test_dates[0], test_dates[-1]

    try:
        # 一次预加载覆盖训练段+测试段，测试段首日的长周期指标已在训练段数据上预热
        preloaded = preload_backtest_data([ts_code], train_start, test_end)
    except Exception as e:
        logger.warning(f"预加载标的 {ts_code} 数据失败: {e}")
        preloaded = None

    empty_stats = {"total_return": 0.0, "win_rate": 0.0, "trades": 0,
                   "sells": 0, "mdd": 0.0, "sharpe": 0.0}

    def _run(s_name: str, s_start: str, s_end: str) -> dict:
        s_cls = STRATEGY_REGISTRY[s_name]
        params = {k: v.get("default", 0) for k, v in s_cls.param_schema.items()}
        rm = RiskManager(stop_loss_pct=-6.0, trailing_stop_activation=12.0,
                         trailing_stop_callback=3.0)
        bt = Backtester(
            strategy_cls=s_cls, params=params, universe=[ts_code],
            start_date=s_start, end_date=s_end, initial_capital=100000.0,
            execution_mode="next_open", risk_manager=rm,
            preloaded_data=preloaded,
        )
        res = bt.run(save=False)
        return {
            "total_return": float(res.total_return),
            "win_rate": float(res.win_rate),
            "trades": int(res.trade_count),
            "sells": int(getattr(res, "sell_count", 0)),
            "mdd": float(res.max_drawdown),
            "sharpe": float(res.sharpe_ratio),
        }

    stats = {}
    strategy_candidates = ["kdj_cross", "bollinger_reversal", "ma_cross", "signal_combo"]
    for s_name in strategy_candidates:
        if s_name not in STRATEGY_REGISTRY:
            continue
        try:
            stats[s_name] = _run(s_name, train_start, train_end)
        except Exception as e:
            logger.warning(f"标的 {ts_code} 训练段回测策略 {s_name} 异常: {e}")
            stats[s_name] = dict(empty_stats)

    if not stats:
        return {
            "strategy": "kdj_cross", "strategy_cn": PREFIX_MAP["kdj_cross"],
            "tag": "⚡ KDJ短线企稳", "reason": "策略回测无有效结果，采用默认推荐。",
            "details": {}, "is_backtested": False, "is_oos": False,
        }

    # 综合效用评分（仅在训练段上选策略）：
    # 评分 = 收益*1.5 + (胜率-40)*0.6 - 最大回撤*0.4
    # 无任何平仓成交的策略给予极低哨兵值，避免与正常策略混淆
    best_s, best_score = None, -1e12
    for s_name, m in stats.items():
        if m["sells"] == 0 and m["trades"] == 0:
            score = -1e9
        else:
            score = (
                m["total_return"] * 1.5 + (m["win_rate"] - 40.0) * 0.6 - m["mdd"] * 0.4
            )
        if score > best_score:
            best_score, best_s = score, s_name
    if best_s is None:
        best_s = "kdj_cross"

    prefix = PREFIX_MAP.get(best_s, best_s)
    # 测试段冻结策略，只用样本外业绩生成标签与理由
    try:
        oos = _run(best_s, test_start, test_end)
    except Exception as e:
        logger.warning(f"标的 {ts_code} 样本外回测失败: {e}")
        oos = dict(empty_stats)

    # 胜率仅由已平仓卖单计算，因此样本外必须满足最低平仓笔数 (sells >= OOS_MIN_TRADES)
    if oos["sells"] >= OOS_MIN_TRADES:
        if oos["win_rate"] >= 55.0:
            tag = f"{prefix} (样本外胜率{oos['win_rate']:.1f}%/{oos['total_return']:+.1f}%)"
        else:
            tag = f"{prefix} (样本外{oos['total_return']:+.1f}%/回撤{oos['mdd']:.1f}%)"
    else:
        tag = f"{prefix} (样本外交易不足)"

    reason = (
        f"训练段 {train_start}~{train_end} 比较 {len(stats)} 个策略后选出该策略；"
        f"以下业绩来自从未参与选参的样本外测试段 {test_start}~{test_end}："
        f"总收益 {oos['total_return']:+.2f}%，胜率 {oos['win_rate']:.1f}%，"
        f"最大回撤 {oos['mdd']:.2f}%，平仓 {oos['sells']} 笔（总成交 {oos['trades']} 次）。"
        f"（details 中的分策略对比为样本内结果，不可当作业绩）"
    )

    return {
        "strategy": best_s,
        "strategy_cn": prefix,
        "tag": tag,
        "reason": reason,
        "details": stats,
        "is_backtested": True,
        "is_oos": True,
        "train_window": [train_start, train_end],
        "test_window": [test_start, test_end],
        "oos_total_return": oos["total_return"],
        "oos_win_rate": oos["win_rate"],
        "oos_trades": oos["trades"],
    }


def batch_update_watchlist_recommendations(force: bool = False,
                                           progress_callback = None,
                                           start_date: str = "20230101",
                                           end_date: str = "") -> int:
    """批量更新所有自选股的推荐策略标签

    参数:
        force: 若为 True，则重新评估全部自选股；若为 False，仅评估尚无推荐标签的标的
        progress_callback: 进度回调 (current, total, ts_code, name)
        start_date / end_date: 统一评估窗口。批量评估必须共用同一终点，
            否则每只股票各自取「今天」，样本外窗口长度不可比。

    返回:
        更新成功的标的数量
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")
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

        res = evaluate_stock_strategy(code, name=c_name, industry=c_ind,
                                      start_date=start_date, end_date=end_date)
        update_watchlist_recommendation(code, res["tag"])
        updated += 1

    return updated
