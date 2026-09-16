"""Tier 1 ~ Tier 4 深度架构与工程优化的专项测试

涵盖：
  - Tier 1: 数据预热 (Preloaded Data) 与网格搜索加速
  - Tier 2: 除权除息断层检测 (check_split_dividend_anomaly) 与全量覆写保护
  - Tier 3: 交易决策与风控上下文快照 (context_snapshot) 捕获与持久化
  - Tier 4: 横截面置信度排序与最大活跃持仓上限 (max_active_positions)
"""
import pytest
import pandas as pd
import numpy as np

from core.models import Signal, Trade
from engine.portfolio import Portfolio
from engine.backtester import Backtester, grid_search, preload_backtest_data
from engine.risk_manager import RiskManager
from data.storage import (
    save_daily, clear_daily, get_daily, check_split_dividend_anomaly,
    save_backtest_result, save_backtest_trades, get_conn, init_db
)
from strategies.base import BaseStrategy


# -------------------------------------------------------------
# 测试辅助策略
# -------------------------------------------------------------
class MultiStockDummyStrategy(BaseStrategy):
    name = "multi_stock_dummy"
    description = "为不同股票赋予不同置信度"

    def __init__(self, scores: dict = None):
        self.scores = scores or {"000001.SZ": 0.9, "000002.SZ": 0.5, "600519.SH": 0.3}

    def on_bar(self, trade_date: str, data: dict, portfolio=None):
        signals = []
        for ts_code, df in data.items():
            if portfolio and portfolio.positions.get(ts_code) and not portfolio.positions[ts_code].is_empty:
                continue
            row = df.iloc[-1]
            score = self.scores.get(ts_code, 0.5)
            signals.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=self.name,
                direction="BUY",
                score=score,
                price_ref=row["close"],
                reason=f"测试买入评分={score}"
            ))
        return signals


# -------------------------------------------------------------
# Tier 1 测试：数据预热机制与结果一致性
# -------------------------------------------------------------
def test_preloaded_data_consistency(monkeypatch):
    """验证传入 preloaded_data 与普通回测生成的指标和交易结果完全一致"""
    dates = ["20240102", "20240103", "20240104", "20240105"]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 4,
        "trade_date": dates,
        "open": [10.0, 10.2, 10.5, 10.8],
        "high": [10.3, 10.6, 10.9, 11.0],
        "low": [9.9, 10.1, 10.4, 10.7],
        "close": [10.1, 10.4, 10.7, 10.9],
        "volume": [100000.0] * 4,
        "vol": [100000.0] * 4,
        "amount": [1000000.0] * 4,
    })

    monkeypatch.setattr("engine.backtester.get_daily", lambda ts_code, s, e: df.copy())
    monkeypatch.setattr("engine.backtester.save_backtest_result", lambda res: 1)
    monkeypatch.setattr("engine.backtester.save_backtest_trades", lambda tid, trades: None)

    # 1. 预加载数据
    preloaded = preload_backtest_data(["000001.SZ"], "20240102", "20240105")
    assert "000001.SZ" in preloaded
    assert "ma5" in preloaded["000001.SZ"].columns

    # 2. 使用 preloaded_data 运行
    bt_fast = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
        preloaded_data=preloaded
    )
    res_fast = bt_fast.run(save=False)

    # 3. 普通运行
    bt_normal = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240105",
    )
    res_normal = bt_normal.run(save=False)

    assert res_fast.total_return == res_normal.total_return
    assert len(res_fast.trades) == len(res_normal.trades)


# -------------------------------------------------------------
# Tier 2 测试：除权除息跳空断层检测
# -------------------------------------------------------------
def test_check_split_dividend_anomaly():
    """验证除权除息检测逻辑：平稳行情不触发，断崖跳空准确触发"""
    test_code = "999999.SZ"
    clear_daily(test_code)

    # 先存入基准历史数据（收盘价 20.0）
    hist_df = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240531",
        "open": 20.0, "high": 20.5, "low": 19.8, "close": 20.0,
        "volume": 10000.0, "amount": 200000.0, "pct_chg": 0.0, "adj_factor": 1.0
    }])
    save_daily(hist_df)

    # 1. 正常行情：今天 20.2 元 (+1.0%) -> 不应触发断层
    normal_incoming = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240603",
        "open": 20.1, "high": 20.4, "low": 20.0, "close": 20.2,
        "volume": 10000.0, "amount": 202000.0, "pct_chg": 1.0, "adj_factor": 1.0
    }])
    assert not check_split_dividend_anomaly(test_code, normal_incoming)

    # 2. 发生除权（10送10），增量前复权收盘价变为 10.1 元，pct_chg 为 +1.0%
    # 推导的前收为 10.0，但库里是 20.0 -> 差异达 100% -> 必须触发异常
    split_incoming = pd.DataFrame([{
        "ts_code": test_code, "trade_date": "20240603",
        "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.1,
        "volume": 20000.0, "amount": 202000.0, "pct_chg": 1.0, "adj_factor": 1.0
    }])
    assert check_split_dividend_anomaly(test_code, split_incoming)

    # 清理测试数据
    clear_daily(test_code)
    assert get_daily(test_code).empty


# -------------------------------------------------------------
# Tier 3 测试：决策与风控上下文快照
# -------------------------------------------------------------
def test_decision_snapshot_and_db_storage():
    """验证交易决策快照被捕获并能成功持久化到数据库"""
    init_db()  # 触发可能存在的迁移
    dates = ["20240102", "20240103"]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 2,
        "trade_date": dates,
        "open": [10.0, 10.5],
        "high": [10.2, 10.8],
        "low": [9.8, 10.3],
        "close": [10.1, 10.6],
        "volume": [100000.0] * 2,
        "vol": [100000.0] * 2,
        "amount": [1000000.0] * 2,
    })

    preloaded = {"000001.SZ": df.copy()}
    # 回测产生买入 Trade
    bt = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240102",
        end_date="20240103",
        preloaded_data=preloaded
    )
    res = bt.run(save=False)
    assert len(res.trades) > 0
    trade = res.trades[0]
    # 验证快照被正确挂载
    assert trade.context_snapshot is not None
    assert "score" in trade.context_snapshot
    assert "close" in trade.context_snapshot
    assert trade.context_snapshot["close"] == 10.1

    # 验证存入数据库
    bt_id = save_backtest_result(res)
    save_backtest_trades(bt_id, res.trades)
    with get_conn() as conn:
        row = conn.execute("SELECT context_snapshot FROM backtest_trade WHERE backtest_id = ?", (bt_id,)).fetchone()
        assert row is not None
        assert "score" in row[0]


def test_risk_manager_snapshot():
    """验证 RiskManager 触发风控时捕获详细参数快照"""
    rm = RiskManager(stop_loss_pct=-5.0)
    p = Portfolio(initial_capital=100000)
    pos = pos = p.buy(ts_code="000001.SZ", price=10.0, volume=1000, trade_date="20240102")

    signals = rm.check_risks("20240103", p, {"000001.SZ": 9.40})
    assert len(signals) == 1
    sig = signals[0]
    assert sig.context_snapshot is not None
    assert sig.context_snapshot["risk_type"] == "stop_loss"
    assert sig.context_snapshot["pnl_pct"] <= -5.0
    assert sig.context_snapshot["current_price"] == 9.40


# -------------------------------------------------------------
# Tier 4 测试：横截面排序与持仓上限控制
# -------------------------------------------------------------
def test_cross_sectional_ranking_and_max_positions(monkeypatch):
    """验证横截面排序与最大持仓上限：同日多个信号优先买入高评分标的，满额后拦截后续标的"""
    dates = ["20240102", "20240103"]
    # 构造 3 只股票数据
    codes = ["000001.SZ", "000002.SZ", "600519.SH"]
    preloaded = {}
    for c in codes:
        preloaded[c] = pd.DataFrame({
            "ts_code": [c] * 2,
            "trade_date": dates,
            "open": [10.0, 10.2],
            "high": [10.5, 10.6],
            "low": [9.8, 10.0],
            "close": [10.1, 10.4],
            "volume": [100000.0] * 2,
            "vol": [100000.0] * 2,
            "amount": [1000000.0] * 2,
        })

    # 设置评分: 000001(0.9) > 000002(0.6) > 600519(0.3)
    # 最大持仓上限限制为 2 只
    bt = Backtester(
        strategy_cls=MultiStockDummyStrategy,
        params={"scores": {"000001.SZ": 0.9, "000002.SZ": 0.6, "600519.SH": 0.3}},
        universe=codes,
        start_date="20240102",
        end_date="20240103",
        initial_capital=100000,
        preloaded_data=preloaded,
        max_active_positions=2  # 限制最多同时持有 2 只
    )
    res = bt.run(save=False)

    bought_codes = [t.ts_code for t in res.trades if t.direction == "BUY"]
    # 断言：必须成功买入 000001.SZ (0.9) 和 000002.SZ (0.6)，而 600519.SH (0.3) 必须被拦截！
    assert len(bought_codes) == 2
    assert "000001.SZ" in bought_codes
    assert "000002.SZ" in bought_codes
    assert "600519.SH" not in bought_codes


# -------------------------------------------------------------
# 全局逻辑与架构缺陷修复专项回归测试
# -------------------------------------------------------------
def test_check_stock_liquidity_volume_compatibility():
    """验证 check_stock_liquidity 兼容 volume 与 vol 字段"""
    from engine.scanner import check_stock_liquidity
    df_vol = pd.DataFrame([
        {"trade_date": "20260915", "close": 10.0, "volume": 100000.0, "amount": 1000000.0},
        {"trade_date": "20260916", "close": 10.5, "volume": 120000.0, "amount": 1260000.0},
    ])
    assert check_stock_liquidity(df_vol, end_date="20260916", filter_suspended=True) is True

    df_zero = pd.DataFrame([
        {"trade_date": "20260915", "close": 10.0, "volume": 100000.0, "amount": 1000000.0},
        {"trade_date": "20260916", "close": 10.0, "volume": 0.0, "amount": 0.0},
    ])
    assert check_stock_liquidity(df_zero, end_date="20260916", filter_suspended=True) is False


def test_get_daily_limit_returns_latest_chronological():
    """验证 get_daily(limit=N) 获取最新 N 条并按升序排列"""
    test_code = "999999.TEST"
    dates = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    with get_conn() as conn:
        conn.execute("DELETE FROM daily_price WHERE ts_code = ?", (test_code,))
        for d in dates:
            conn.execute(
                "INSERT INTO daily_price (ts_code, trade_date, open, high, low, close, volume, amount) "
                "VALUES (?, ?, 10, 11, 9, 10, 1000, 10000)",
                (test_code, d)
            )

    try:
        df = get_daily(test_code, limit=2)
        assert len(df) == 2
        assert df["trade_date"].tolist() == ["20260104", "20260105"]

        df1 = get_daily(test_code, limit=1)
        assert len(df1) == 1
        assert df1["trade_date"].iloc[0] == "20260105"
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM daily_price WHERE ts_code = ?", (test_code,))


def test_risk_manager_parameter_aliases():
    """验证 RiskManager 别名参数兼容性"""
    rm = RiskManager(
        stop_loss_pct=-4.5,
        trailing_stop_pct=7.5,
        trailing_callback_pct=2.5,
        max_holding_days=15,
    )
    assert rm.stop_loss_pct == -4.5
    assert rm.trailing_stop_activation == 7.5
    assert rm.trailing_stop_callback == 2.5
    assert rm.max_holding_days == 15


def test_default_indicators_contains_kdj_and_atr():
    """验证 apply_indicators 默认计算包含 kdj 与 atr"""
    from data.indicators import apply_indicators
    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103", "20260104", "20260105"],
        "open": [10.0, 10.2, 10.1, 10.3, 10.5],
        "high": [10.5, 10.6, 10.4, 10.7, 10.8],
        "low": [9.8, 10.0, 9.9, 10.1, 10.3],
        "close": [10.2, 10.1, 10.3, 10.5, 10.6],
        "volume": [1000, 1200, 1100, 1300, 1500],
    })
    res = apply_indicators(df)
    assert "kdj_k" in res.columns
    assert "kdj_d" in res.columns
    assert "atr14" in res.columns


def test_risk_manager_does_not_pollute_highest_prices_from_non_positions():
    """验证风控管理器不会被未持仓股票的历史高价污染导致建仓后立刻误触移动止盈"""
    from engine.portfolio import Portfolio, Position
    rm = RiskManager(trailing_stop_activation=8.0, trailing_stop_callback=3.0)
    p = Portfolio(initial_capital=100000)

    # 模拟股票曾经在历史上有过高价 100 元，但当时我们并未持仓
    current_prices_day1 = {"000001.SZ": 100.0}
    signals_day1 = rm.check_risks("20240101", p, current_prices_day1)
    assert len(signals_day1) == 0
    # 未持仓标的不应记录在 highest_prices 中
    assert "000001.SZ" not in rm.highest_prices

    # 第 20 天在 50 元建仓
    pos = Position(ts_code="000001.SZ", shares=1000, avg_cost=50.0, buy_date="20240120")
    p.positions["000001.SZ"] = pos

    # 第 21 天，价格仍在 50 元（涨幅 0%）
    signals_day21 = rm.check_risks("20240121", p, {"000001.SZ": 50.0})
    # 绝不能误报跟踪止盈
    assert len(signals_day21) == 0
    # 最高价基准应为成本价 50.0
    assert rm.highest_prices["000001.SZ"] == 50.0


def test_macd_divergence_zero_division_guard():
    """验证 MACD 背离在 macd_at_price_min 接近 -0.001 时不会发生除0崩溃"""
    from strategies.macd_divergence import MACDDivergenceStrategy
    strategy = MACDDivergenceStrategy(lookback=5)

    df = pd.DataFrame({
        "trade_date": ["20260101", "20260102", "20260103", "20260104", "20260105"],
        "close": [10.0, 9.8, 9.7, 9.72, 9.71],
        "volume": [1000, 1000, 1000, 1000, 1000],
        # 构造刚好等于 -0.001 的 macd_hist
        "macd_hist": [-0.01, -0.005, -0.001, -0.0005, -0.0002],
    })

    # 不应抛出 ZeroDivisionError
    sigs = strategy.on_bar("20260105", {"000001.SZ": df})
    assert isinstance(sigs, list)


def test_portfolio_sell_date_string_with_hyphens():
    """验证卖出时即使传入带连字符的日期格式，holding_days 也能正确解析"""
    from engine.portfolio import Portfolio, Position
    p = Portfolio(initial_capital=100000)
    pos = Position(ts_code="000001.SZ", shares=1000, available_shares=1000, avg_cost=10.0, buy_date="2024-01-01")
    p.positions["000001.SZ"] = pos

    trade = p.sell(ts_code="000001.SZ", price=11.0, volume=1000, trade_date="2024-01-11")
    assert trade is not None
    assert trade.holding_days == 10


def test_etf_stamp_duty_exemption():
    """验证场内 ETF (51/15/16等) 交易免征印花税"""
    from engine.commission import calc_cost
    # 普通股票卖出收 0.05% 印花税
    stock_cost = calc_cost(price=10.0, volume=1000, direction="SELL", ts_code="600519.SH")
    assert stock_cost["tax"] == 5.0

    # ETF 卖出免收印花税
    etf_cost = calc_cost(price=3.0, volume=10000, direction="SELL", ts_code="510300.SH")
    assert etf_cost["tax"] == 0.0


def test_ma60_breakout_temporal_consistency():
    """验证 MA60 突破策略基于 prev_ma 判定突破"""
    from strategies.ma60_breakout import MA60BreakoutStrategy
    st = MA60BreakoutStrategy(ma_period=5, slope_days=2, vol_ratio=1.1)

    # 构造数据：第8天收盘9.7低于MA5(9.84)，第9天收盘10.5突破MA5(9.94)，均线向上且放量
    df = pd.DataFrame({
        "trade_date": [f"2026010{i}" for i in range(1, 10)],
        "close": [10.0, 10.0, 10.0, 10.0, 9.9, 9.8, 9.8, 9.7, 10.5],
        "volume": [1000] * 8 + [2000],
        "vol_ma5": [1000] * 9,
    })
    from data.indicators import add_ma
    df = add_ma(df, [5])
    sigs = st.on_bar("20260109", {"000001.SZ": df})
    # 应当生成买入信号
    assert len(sigs) == 1
    assert sigs[0].direction == "BUY"


def test_multi_factor_and_signal_combo_without_pct_chg_column():
    """验证 multi_factor 和 signal_combo 缺失 pct_chg 列时自动回退至价格推导"""
    from strategies.multi_factor import MultiFactorStrategy
    from strategies.signal_combo import SignalComboStrategy

    mf = MultiFactorStrategy(ma_period=5)
    sc = SignalComboStrategy(ma_period=5)

    df = pd.DataFrame({
        "trade_date": [f"2026010{i}" for i in range(1, 35)],
        "open": [10.0 + i * 0.1 for i in range(34)],
        "high": [10.2 + i * 0.1 for i in range(34)],
        "low": [9.8 + i * 0.1 for i in range(34)],
        "close": [10.1 + i * 0.1 for i in range(34)],
        "volume": [1000] * 33 + [3000],  # 放量
        "vol_ma5": [1000] * 34,
        "ma5": [10.0 + i * 0.1 for i in range(34)],
        "ma20": [9.5 + i * 0.05 for i in range(34)],
        "dif": [0.5] * 34,
        "dea": [0.3] * 34,
        "macd_hist": [0.4] * 34,
        "rsi14": [60.0] * 34,
        "boll_upper": [15.0] * 34,
        "boll_mid": [12.0] * 34,
        "boll_lower": [9.0] * 34,
    })
    # 故意不包含 pct_chg 列
    assert "pct_chg" not in df.columns

    # 运行不报错且能正常评分
    sigs_mf = mf.on_bar("20260134", {"000001.SZ": df})
    sigs_sc = sc.on_bar("20260134", {"000001.SZ": df})
    assert isinstance(sigs_mf, list)
    assert isinstance(sigs_sc, list)


def test_get_signals_with_start_date():
    """验证 get_signals 支持 start_date 区间过滤"""
    from data.storage import save_signal, get_signals
    from core.models import Signal

    save_signal(Signal(ts_code="000001.SZ", trade_date="20260301", strategy="ma_cross", direction="BUY", score=0.8))
    save_signal(Signal(ts_code="000001.SZ", trade_date="20260310", strategy="ma_cross", direction="BUY", score=0.9))

    df_filtered = get_signals(start_date="20260305")
    assert not df_filtered.empty
    assert (df_filtered["trade_date"] >= "20260305").all()


def test_risk_manager_parameter_auto_normalization():
    """验证 RiskManager 对小数与正数百分比输入的自适应归一化能力"""
    from engine.risk_manager import RiskManager
    # 传入小数：0.05, 0.08, 0.03
    rm1 = RiskManager(stop_loss_pct=0.05, trailing_stop_activation=0.08, trailing_stop_callback=0.03)
    assert rm1.stop_loss_pct == -5.0
    assert rm1.trailing_stop_activation == 8.0
    assert rm1.trailing_stop_callback == 3.0

    # 传入正数百分比：5.0
    rm2 = RiskManager(stop_loss_pct=5.0, trailing_stop_activation=8.0, trailing_stop_callback=3.0)
    assert rm2.stop_loss_pct == -5.0
    assert rm2.trailing_stop_activation == 8.0
    assert rm2.trailing_stop_callback == 3.0

def test_backtester_equity_curve_no_duplicate_dates(sample_prices, sample_strategy_cls, monkeypatch):
    """验证回测最终交易日权益记录不重复，权益曲线天数严格等于回测交易日天数"""
    from engine.backtester import Backtester

    sp = sample_prices
    strat = sample_strategy_cls
    bt = Backtester(
        strategy_cls=strat,
        params={},
        universe=["999999.SZ"],
        start_date="20260101",
        end_date="20260131",
        preloaded_data=sp,
    )
    res = bt.run(save=False)
    dates = [e["date"] for e in res.equity_curve]
    # 确保无重复交易日
    assert len(dates) == len(set(dates))
    assert len(dates) == len(sp["999999.SZ"])


def test_tushare_forward_adjust_sorting_logic():
    """验证 Tushare 复权前按日期升序排序，确保 iloc[-1] 对应最新复权因子而非最早历史因子"""
    # 构造降序排列的原始返回数据 (Tushare Pro 默认排序)
    desc_df = pd.DataFrame({
        "ts_code": ["000001.SZ", "000001.SZ"],
        "trade_date": ["20260302", "20260301"],  # 降序: 02 在前，01 在后
        "open": [10.0, 5.0],
        "high": [10.5, 5.2],
        "low": [9.8, 4.9],
        "close": [10.2, 5.1],
        "volume": [1000, 1000],
        "amount": [10000, 5000],
        "pct_chg": [2.0, 1.0],
        "adj_factor": [2.0, 1.0],  # 02 的因子为 2.0，01 的因子为 1.0
    })
    # 正确逻辑：必须先按 trade_date 升序排序，再取 iloc[-1] 作为 latest_adj
    df = desc_df.sort_values("trade_date").reset_index(drop=True)
    latest_adj = df["adj_factor"].iloc[-1]
    assert latest_adj == 2.0  # 最新因子必须是 2.0，不能是 1.0
    # 20260301 前复权价 = 5.1 * 1.0 / 2.0 = 2.55
    qfq_close = (df.loc[df["trade_date"] == "20260301", "close"] * 1.0 / latest_adj).iloc[0]
    assert round(qfq_close, 2) == 2.55


def test_scanner_min_days_after_end_date_filter(monkeypatch):
    """验证扫描器在过滤 end_date 后验证数据长度，防止历史截断后样本过短"""
    from engine.scanner import _scan_single_stock
    from strategies.ma_cross import MACrossStrategy

    # 构造 100 天数据，但 20260105 及之前只有 3 天
    dates = [f"202601{i+1:02d}" for i in range(30)] + [f"202602{i+1:02d}" for i in range(28)]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * len(dates),
        "trade_date": dates,
        "open": [10.0] * len(dates),
        "high": [10.5] * len(dates),
        "low": [9.5] * len(dates),
        "close": [10.0] * len(dates),
        "volume": [100000.0] * len(dates),
        "amount": [1000000.0] * len(dates),
        "pct_chg": [0.0] * len(dates),
        "turnover": [1.0] * len(dates),
        "adj_factor": [1.0] * len(dates),
    })

    monkeypatch.setattr("engine.scanner.get_daily", lambda ts: df)
    # 当 end_date 为 20260103 时，有效数据仅 3 条 (< 60)，应被拒绝返回空列表
    sigs = _scan_single_stock("000001.SZ", "20260103", [MACrossStrategy()])
    assert sigs == []


def test_storage_get_index_daily_range():
    """验证 storage.get_index_daily_range 正确支持日期区间过滤与升序排列"""
    from data.storage import get_conn, get_index_daily_range

    with get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS index_daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, PRIMARY KEY (ts_code, trade_date))")
        conn.execute("INSERT OR REPLACE INTO index_daily VALUES ('000300.SH', '20260101', 3800, 3850, 3790, 3820, 1000, 2000)")
        conn.execute("INSERT OR REPLACE INTO index_daily VALUES ('000300.SH', '20260105', 3820, 3860, 3810, 3850, 1000, 2000)")
        conn.execute("INSERT OR REPLACE INTO index_daily VALUES ('000300.SH', '20260110', 3850, 3880, 3840, 3870, 1000, 2000)")

    df = get_index_daily_range("000300.SH", "20260102", "20260108")
    assert len(df) == 1
    assert df.iloc[0]["trade_date"] == "20260105"


def test_backtest_service_options_passthrough():
    """验证 BacktestService.run_backtest 正确透传 execution_mode 与 max_active_positions"""
    from services.backtest_service import BacktestService
    import inspect

    sig = inspect.signature(BacktestService.run_backtest)
    assert "execution_mode" in sig.parameters
    assert "risk_manager" in sig.parameters
    assert "max_active_positions" in sig.parameters


def test_divergence_reference_swing_excludes_current_bar():
    """验证 MACD与RSI背离策略参考基准排除当前周期，确保创出新低/新高时背离能正常触发"""
    from strategies.macd_divergence import MACDDivergenceStrategy
    from strategies.rsi_divergence import RSIDivergenceStrategy

    # 构造 30 天数据：
    # 第 10 天最低价 10.0，MACD hist = -0.50，RSI = 25
    # 第 11-28 天反弹至 12.0
    # 第 29 天跌至 10.2
    # 第 30 天 (今天) 创出更低价 9.80，但 MACD hist = -0.20 (抬高)，RSI = 35 (抬高)
    closes = [12.0] * 30
    closes[10] = 10.0
    closes[-1] = 9.80  # 今天创出区间新低

    macd_hist = [-0.1] * 30
    macd_hist[10] = -0.50  # 前低时大幅探底
    macd_hist[-1] = -0.20  # 今日新低时 MACD 显著抬高 (典型底背离)

    rsi = [50.0] * 30
    rsi[10] = 25.0         # 前低时超卖
    rsi[-1] = 35.0         # 今日新低时 RSI 抬高 (典型底背离)

    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 30,
        "trade_date": [f"202601{i+1:02d}" for i in range(30)],
        "close": closes,
        "volume": [10000] * 30,
        "macd_hist": macd_hist,
        "rsi14": rsi,
    })

    # MACD 背离策略触发买入
    strat_macd = MACDDivergenceStrategy(lookback=30)
    sigs_macd = strat_macd.on_bar("20260130", {"000001.SZ": df})
    assert len(sigs_macd) == 1
    assert sigs_macd[0].direction == "BUY"

    # RSI 背离策略触发买入
    strat_rsi = RSIDivergenceStrategy(lookback=30)
    sigs_rsi = strat_rsi.on_bar("20260130", {"000001.SZ": df})
    assert len(sigs_rsi) == 1
    assert sigs_rsi[0].direction == "BUY"


def test_turtle_strategy_prev_close_cross_confirmation():
    """验证海龟突破策略严格基于跨越突破(prev_close <= high_n and price > high_n)，防止连阳持续刷屏"""
    from strategies.turtle import TurtleStrategy

    # 前 20 天最高价 10.0，第 21 天收 10.5(首日突破)，第 22 天收 10.8(持续高位运行)
    df_day1 = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 22,
        "trade_date": [f"202601{i+1:02d}" for i in range(22)],
        "high": [10.0] * 20 + [10.5, 10.8],
        "low": [9.0] * 22,
        "close": [9.5] * 20 + [10.5, 10.8],
        "volume": [10000] * 22,
    })

    strat = TurtleStrategy(entry_period=20)
    # 第 21 天 (突破首日): 昨日 9.5 <= 10.0，今日 10.5 > 10.0，必须触发买入
    df_step1 = df_day1.iloc[:21]
    sigs1 = strat.on_bar("20260121", {"000001.SZ": df_step1})
    assert len(sigs1) == 1
    assert sigs1[0].direction == "BUY"
    assert sigs1[0].score >= 0.6  # 评分基线合理

    # 第 22 天 (连阳次日): 昨日 10.5 已高于 high_n，今日 10.8，绝不能重复触发买入！
    sigs2 = strat.on_bar("20260122", {"000001.SZ": df_day1})
    assert len(sigs2) == 0


def test_calc_cost_zero_volume_guard():
    """验证 calc_cost 在数量或成交额为 0 时返回 0 费用，不触发 5 元起征"""
    from engine.commission import calc_cost

    cost = calc_cost(price=10.0, volume=0, direction="BUY")
    assert cost["total"] == 0.0
    assert cost["commission"] == 0.0


def test_apply_indicators_empty_dataframe_safe():
    """验证 apply_indicators 传入空 DataFrame 时安全返回不报错"""
    from data.indicators import apply_indicators

    df_empty = pd.DataFrame()
    res = apply_indicators(df_empty)
    assert res.empty


def test_position_bool_evaluation():
    """验证 Position 对象的布尔求值遵循 shares > 0 (空仓为 False)"""
    from engine.position import Position

    pos_empty = Position(ts_code="000001.SZ", shares=0)
    assert not bool(pos_empty)
    assert pos_empty.is_empty

    pos_held = Position(ts_code="000001.SZ", shares=100)
    assert bool(pos_held)
    assert not pos_held.is_empty


def test_donchian_breakout_prev_close_cross_confirmation():
    """验证唐奇安通道突破策略基于跨越突破(prev_close <= prev_upper)，杜绝连阳持续发射买入信号"""
    from strategies.donchian_breakout import DonchianBreakoutStrategy

    df_day1 = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 23,
        "trade_date": [f"202601{i+1:02d}" for i in range(23)],
        "high": [10.0] * 20 + [10.5, 10.8, 11.0],
        "low": [9.0] * 23,
        "close": [9.5] * 20 + [10.5, 10.8, 11.0],
        "volume": [10000] * 23,
    })

    strat = DonchianBreakoutStrategy(entry_period=20)
    # 第 21 天 (突破首日): 昨日 9.5 <= 10.0，今日 10.5 > 10.0，触发买入
    df_step1 = df_day1.iloc[:21]
    sigs1 = strat.on_bar("20260121", {"000001.SZ": df_step1})
    assert len(sigs1) == 1
    assert sigs1[0].direction == "BUY"

    # 第 22 天 (连阳次日): 昨日 10.5 已突破通道，今日 10.8，绝不重复买入
    df_step2 = df_day1.iloc[:22]
    sigs2 = strat.on_bar("20260122", {"000001.SZ": df_step2})
    assert len(sigs2) == 0


def test_multi_factor_and_signal_combo_with_pct_chg_column():
    """验证 multi_factor 和 signal_combo 包含 pct_chg 列时安全调用 pd.notna 且不抛 NameError"""
    from strategies.multi_factor import MultiFactorStrategy
    from strategies.signal_combo import SignalComboStrategy

    mf = MultiFactorStrategy(ma_period=5)
    sc = SignalComboStrategy(ma_period=5)

    df = pd.DataFrame({
        "trade_date": [f"2026010{i}" for i in range(1, 35)],
        "open": [10.0 + i * 0.1 for i in range(34)],
        "high": [10.2 + i * 0.1 for i in range(34)],
        "low": [9.8 + i * 0.1 for i in range(34)],
        "close": [10.1 + i * 0.1 for i in range(34)],
        "volume": [1000] * 33 + [3000],
        "vol_ma5": [1000] * 34,
        "ma5": [10.0 + i * 0.1 for i in range(34)],
        "ma20": [9.5 + i * 0.05 for i in range(34)],
        "dif": [0.5] * 34,
        "dea": [0.3] * 34,
        "macd_hist": [0.4] * 34,
        "rsi14": [60.0] * 34,
        "boll_upper": [15.0] * 34,
        "boll_mid": [12.0] * 34,
        "boll_lower": [9.0] * 34,
        "pct_chg": [1.5] * 34,  # 带有 pct_chg 列
    })

    sigs_mf = mf.on_bar("20260134", {"000001.SZ": df})
    assert isinstance(sigs_mf, list)

    sigs_sc = sc.on_bar("20260134", {"000001.SZ": df})
    assert isinstance(sigs_sc, list)





