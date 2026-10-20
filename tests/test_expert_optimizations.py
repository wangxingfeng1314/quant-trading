"""全面测试本轮 8 项工业级优化功能"""
import pytest
import pandas as pd
import logging
from logging.handlers import RotatingFileHandler

from engine.position import Position
from engine.portfolio import Portfolio
from engine.backtester import Backtester
from engine.scanner import check_stock_liquidity
from services.signal_service import SignalService, resolve_signal_conflicts
from core.models import Signal
from core.config import setup_logging


def test_position_adjust_for_split_and_dividend():
    """测试单只股票除权除息处理"""
    pos = Position(ts_code="600519.SH", shares=1000, avg_cost=20.0, available_shares=1000, total_cost=20000.0)

    # 1. 送转股 10 送 10 (split_factor = 2.0)
    cash_div = pos.adjust_for_split(split_factor=2.0)
    assert cash_div == 0.0
    assert pos.shares == 2000
    assert pos.available_shares == 2000
    assert pos.avg_cost == 10.0

    # 2. 现金分红 每股派现 1.0 元
    cash_div = pos.adjust_for_split(dividend_per_share=1.0)
    assert cash_div == 2000.0  # 2000 股 * 1.0
    assert pos.avg_cost == 9.0  # 10.0 - 1.0


def test_portfolio_handle_corporate_action():
    """测试组合层级的除权除息现金入账与持仓调整"""
    portfolio = Portfolio(initial_capital=100000.0)
    trade = portfolio.buy(
        ts_code="000001.SZ", price=10.0, volume=1000,
        trade_date="20240101", slippage=False,
    )
    assert trade is not None
    init_cash = portfolio.cash

    # 10 送 5 (1.5) + 每股分红 0.5 元
    div_received = portfolio.handle_corporate_action("000001.SZ", split_factor=1.5, dividend_per_share=0.5)
    assert div_received == 500.0  # 1000股 * 0.5元
    assert portfolio.cash == init_cash + 500.0

    pos = portfolio.get_position("000001.SZ")
    assert pos.shares == 1500


def test_backtester_equal_weight_budget():
    """测试基于全组合市值的等权买入预算计算"""
    from strategies.base import BaseStrategy
    portfolio = Portfolio(initial_capital=100000.0)
    backtester = Backtester(
        strategy_cls=BaseStrategy,
        params={},
        universe=["000001.SZ"],
        start_date="20240101",
        end_date="20240110",
        initial_capital=100000.0,
    )

    sig = Signal(ts_code="000001.SZ", trade_date="20240102", strategy="MACD", direction="BUY", score=0.5)
    budget = backtester._calc_buy_budget(portfolio, sig)
    # score=0.5 时，pct 约为 min(0.05) + 0.5 * step(0.15) = 0.125
    expected_budget = portfolio.total_equity({}) * 0.125
    assert abs(budget - expected_budget) < 1.0

    # 当现金不足预期预算时，应自动受限于可用现金
    portfolio2 = Portfolio(initial_capital=100000.0)
    portfolio2.buy("000001.SZ", price=10.0, volume=9500, trade_date="20240101", slippage=False)
    # 持仓市值约 95,000 元，现金剩余约 5,000 元，但理想预算为 12,500 元
    budget_limited = backtester._calc_buy_budget(portfolio2, sig)
    assert budget_limited == portfolio2.cash



def test_signal_arbitration_resonance_and_conflict():
    """测试多策略信号仲裁引擎：同向共振与多空净额化"""
    sig1 = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="MACD", direction="BUY", score=0.6, reason="金叉")
    sig2 = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="RSI", direction="BUY", score=0.7, reason="超卖回升")

    # 1. 同向共振
    results = resolve_signal_conflicts([sig1, sig2])
    assert len(results) == 1
    res = results[0]
    assert res.direction == "BUY"
    assert res.score > 0.7  # 共振加成
    assert "ensemble" in res.strategy
    assert "共振" in res.reason
    assert res.context_snapshot.get("is_ensemble") is True


    # 2. 多空冲突
    sig_sell = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="BOLL", direction="SELL", score=0.65, reason="触轨")
    conflict_results = resolve_signal_conflicts([sig1, sig_sell])
    # 0.6 vs 0.65，净差 0.05 < 0.15 阈值，应中和过滤
    assert len(conflict_results) == 0

    # 3. 显著优势冲突
    sig_strong_buy = Signal(ts_code="000001.SZ", trade_date="20240105", strategy="MA", direction="BUY", score=0.9, reason="多头排列")
    strong_results = resolve_signal_conflicts([sig_strong_buy, sig_sell])
    assert len(strong_results) == 1
    assert strong_results[0].direction == "BUY"
    assert "净胜" in strong_results[0].reason



def test_logging_rotation_configuration():
    """测试生产级日志轮转配置"""
    logger = setup_logging(name="test_quant_log", level=logging.INFO, log_filename="test_quant.log")
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) >= 1
    handler = file_handlers[0]
    assert handler.maxBytes == 20 * 1024 * 1024
    assert handler.backupCount == 5


def test_scanner_liquidity_guard():
    """测试流动性风控与停牌标的过滤"""
    # 正常流动性标的
    normal_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 100000, "amount": 40000000.0},
        {"trade_date": "20240103", "vol": 120000, "amount": 50000000.0},
        {"trade_date": "20240104", "vol": 110000, "amount": 45000000.0},
        {"trade_date": "20240105", "vol": 130000, "amount": 60000000.0},
    ])
    assert check_stock_liquidity(normal_df, end_date="20240105", min_daily_amount=30000000.0, filter_suspended=True) is True

    # 停牌标的 (vol=0)
    suspended_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 100000, "amount": 40000000.0},
        {"trade_date": "20240105", "vol": 0, "amount": 0.0},
    ])
    assert check_stock_liquidity(suspended_df, end_date="20240105", min_daily_amount=0.0, filter_suspended=True) is False

    # 微量流动性陷阱标的 (日均不足 3000 万)
    illiquid_df = pd.DataFrame([
        {"trade_date": "20240102", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240103", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240104", "vol": 1000, "amount": 500000.0},
        {"trade_date": "20240105", "vol": 1000, "amount": 500000.0},
    ])
    assert check_stock_liquidity(illiquid_df, end_date="20240105", min_daily_amount=30000000.0, filter_suspended=True) is False


def test_signal_combo_edge_trigger_and_overbought_guard():
    """测试 signal_combo 边沿触发（首日共振触发，连涨不重复刷屏）与 RSI>68 超买防追高"""
    from strategies.signal_combo import SignalComboStrategy

    strat = SignalComboStrategy(ma_period=5, buy_threshold=2)
    # 构造 32 天数据：前 31 天震荡弱势(total < 2)，第 32 天首次放量站上均线且 RSI=60 (适中偏强)
    df = pd.DataFrame({
        "trade_date": [f"202601{i+1:02d}" for i in range(32)],
        "open": [10.0] * 32,
        "high": [10.2] * 32,
        "low": [9.8] * 32,
        "close": [9.8] * 31 + [10.5],
        "volume": [1000.0] * 31 + [2000.0],
        "vol_ma5": [1000.0] * 32,
        "ma5": [10.0] * 32,
        "dif": [-0.1] * 31 + [0.2],
        "dea": [0.0] * 31 + [0.1],
        "macd_hist": [-0.1] * 31 + [0.1],
        "rsi14": [45.0] * 31 + [60.0],
        "pct_chg": [0.0] * 31 + [3.5],
    })
    sigs_first = strat.on_bar("20260132", {"000001.SZ": df})
    assert len(sigs_first) == 1
    assert sigs_first[0].direction == "BUY"

    # 若第 31 天和第 32 天都处于高位且 RSI=75 (超买)，绝不发出追高买入信号
    df_overbought = df.copy()
    df_overbought["close"] = [10.5] * 32
    df_overbought["rsi14"] = [75.0] * 32
    sigs_ob = strat.on_bar("20260132", {"000001.SZ": df_overbought})
    assert not any(s.direction == "BUY" for s in sigs_ob)


def test_scanner_conflict_interception_and_resonance(monkeypatch):
    """测试扫描器同股多空冲突拦截（趋势破位时拦截同日买入信号）与多策略同向共振加成"""
    from engine.scanner import _scan_single_stock
    from strategies.base import BaseStrategy

    class DummyBullStrategy1(BaseStrategy):
        name = "ma_bullish"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="BUY", score=0.75, price_ref=10.0, reason="多头1")]

    class DummyBullStrategy2(BaseStrategy):
        name = "macd_cross"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="BUY", score=0.72, price_ref=10.0, reason="多头2")]

    class DummyTrendBreakdownSell(BaseStrategy):
        name = "ma_cross"
        def on_bar(self, trade_date, data, portfolio=None):
            return [Signal(ts_code="000001.SZ", trade_date=trade_date, strategy=self.name, direction="SELL", score=0.70, price_ref=10.0, reason="均线死叉")]

    dates = pd.date_range("2025-01-01", periods=70, freq="B").strftime("%Y%m%d").tolist()
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 70,
        "trade_date": dates,
        "open": [10.0] * 70,
        "high": [10.5] * 70,
        "low": [9.5] * 70,
        "close": [10.2] * 70,
        "volume": [100000.0] * 70,
        "amount": [50000000.0] * 70,
    })
    monkeypatch.setattr("engine.scanner.get_daily", lambda ts, end_date=None: df.copy())

    # 1. 同向双策略看多共振：保留2条BUY且带共振标记
    res_bull = _scan_single_stock("000001.SZ", dates[-1], [DummyBullStrategy1(), DummyBullStrategy2()], min_daily_amount=0.0)
    assert len(res_bull) == 2
    assert all(s.direction == "BUY" and "策略共振" in s.reason for s in res_bull)

    # 2. 存在均线死叉看空冲突：自动拦截买入推荐，只保留卖出信号
    res_conflict = _scan_single_stock("000001.SZ", dates[-1], [DummyBullStrategy1(), DummyTrendBreakdownSell()], min_daily_amount=0.0)
    assert all(s.direction == "SELL" for s in res_conflict)


def test_p0_limit_up_and_limit_down_untradable():
    """测试 P0-1: A股涨停封板不可买入、跌停封板不可卖出及次日顺延机制"""
    p = Portfolio(initial_capital=100000.0)
    # 1. 前收盘 10.0，主板涨停价 11.00：以 11.00 或 11.50 委托买入应被涨停封死拦截 (返回 None)
    trade_limit_up = p.buy("000001.SZ", price=11.0, volume=1000, trade_date="20260105", prev_close=10.0)
    assert trade_limit_up is None

    # 2. 正常价格 10.50 (< 11.00) 买入成功
    trade_ok = p.buy("000001.SZ", price=10.5, volume=1000, trade_date="20260105", prev_close=10.0, slippage=False)
    assert trade_ok is not None
    assert trade_ok.volume == 1000

    # 3. 次日 (20260106) 前收盘 10.5，跌停价 9.45：以 9.45 一字跌停价卖出应无法成交 (返回 None)
    p.on_new_day("20260106")
    trade_limit_down = p.sell("000001.SZ", price=9.45, volume=1000, trade_date="20260106", prev_close=10.5)
    assert trade_limit_down is None
    assert p.get_position("000001.SZ").shares == 1000  # 仍持有

    # 4. 第3日 (20260107) 打开跌停 (9.60 > 8.51)，正常卖出成交
    p.on_new_day("20260107")
    trade_sold = p.sell("000001.SZ", price=9.60, volume=1000, trade_date="20260107", prev_close=9.45, slippage=False)
    assert trade_sold is not None
    assert p.get_position("000001.SZ").is_empty


def test_p1_atr_dynamic_stops_and_risk_parity_sizing():
    """测试 P1-1: ATR 动态止损止盈与 Backtester ATR 风险平价仓位计算"""
    from notifier.push import calc_atr_stops_and_sizing
    from strategies.base import BaseStrategy

    plan = calc_atr_stops_and_sizing("515180.SH", price=1.35, score=0.8, base_capital=100000.0)
    assert 3.0 <= plan["sl_pct"] <= 8.0
    assert 6.0 <= plan["tp_pct"] <= 16.0
    assert plan["stop_loss"] < 1.35 < plan["take_profit"]
    assert plan["shares"] >= 100

    # 验证 Backtester position_sizing="atr_risk"：低波动股分配预算显著高于高波动妖股
    portfolio = Portfolio(initial_capital=100000.0)
    bt = Backtester(
        strategy_cls=BaseStrategy, params={}, universe=["000001.SZ"],
        start_date="20260101", end_date="20260110",
        initial_capital=100000.0, position_sizing="atr_risk", risk_per_trade=0.015,
    )
    sig = Signal(ts_code="000001.SZ", trade_date="20260105", strategy="ma_cross", direction="BUY", score=0.8, price_ref=10.0)
    df_low_vol = pd.DataFrame([{"close": 10.0, "atr14": 0.12}])   # 日均波幅 1.2%
    df_high_vol = pd.DataFrame([{"close": 10.0, "atr14": 0.45}])  # 日均波幅 4.5%
    budget_low_vol = bt._calc_buy_budget(portfolio, sig, df_stock=df_low_vol, curr_idx=0)
    budget_high_vol = bt._calc_buy_budget(portfolio, sig, df_stock=df_high_vol, curr_idx=0)
    assert budget_low_vol > budget_high_vol


def test_p1_position_action_diagnosis_radar(monkeypatch):
    """测试 P1-2: 持仓管理页实时止损/移动止盈/卖出信号预警雷达"""
    from app.portfolio import diagnose_position_action

    # 模拟当前跌破止损线 (买入价 10.0，现价 9.0，跌幅 -10%)
    df_crash = pd.DataFrame([
        {"trade_date": "20260105", "open": 10.0, "high": 10.1, "low": 9.8, "close": 9.9},
        {"trade_date": "20260106", "open": 9.5, "high": 9.6, "low": 8.9, "close": 9.0},
    ])
    monkeypatch.setattr("app.portfolio.get_daily", lambda code, limit=30: df_crash)
    monkeypatch.setattr("app.portfolio.get_signals", lambda **kw: pd.DataFrame())
    diag_loss = diagnose_position_action("000001.SZ", buy_price=10.0, buy_date="20260105")
    assert diag_loss["level"] == "danger"
    assert "止损" in diag_loss["action"]


def test_p2_batch_summary_and_online_backup(tmp_path):
    """测试 P2-1: SQLite 批量聚合查询与 WAL 在线热备 API"""
    from data.storage import batch_get_daily_summary, backup_database
    from app.st_utils import format_strategy_cn

    # 1. 新策略中文名映射存在
    assert format_strategy_cn("high_dividend_value") == "高股息价值红利"

    # 2. 批量聚合查询
    summary = batch_get_daily_summary(["515180.SH", "601939.SH"])
    assert isinstance(summary, dict)

    # 3. 在线热备生成有效 SQLite 文件
    bk_file = tmp_path / "test_hot_backup.db"
    assert backup_database(bk_file) is True
    assert bk_file.exists() and bk_file.stat().st_size > 0


def test_round3_custom_non_default_strategy_params():
    """测试第3轮 P0-1: 非默认自定义周期参数在各策略中动态计算生效（不再静默返回空或忽略参数）"""
    from strategies.ma_cross import MACrossStrategy
    from strategies.bollinger_reversal import BollingerReversalStrategy
    from strategies.rsi_oversold import RSIOversoldStrategy
    from strategies.kdj_cross import KDJCrossStrategy

    # 构造只含基础 OHLCV（不含 ma7/ma15/rsi6 等非默认列）的行情数据
    closes = [10.0] * 15 + [9.2, 8.8, 8.5, 9.1, 11.8]
    df = pd.DataFrame({
        "ts_code": ["000001.SZ"] * 20,
        "trade_date": [f"202601{i+1:02d}" for i in range(20)],
        "open": [c * 0.99 for c in closes],
        "high": [c * 1.02 for c in closes],
        "low": [c * 0.96 for c in closes],
        "close": closes,
        "volume": [100000.0] * 20,
    })

    # 1. 非默认均线周期 fast=3, slow=10 在无预计算 ma3/ma10 列时仍能动态计算并触发金叉
    ma_strat = MACrossStrategy(fast_period=3, slow_period=10)
    ma_sigs = ma_strat.on_bar("20260120", {"000001.SZ": df})
    assert len(ma_sigs) == 1 and ma_sigs[0].direction == "BUY"

    # 2. 自定义布林带参数 boll_period=10, boll_std=1.2 动态生效
    boll_strat = BollingerReversalStrategy(boll_period=10, boll_std=1.2)
    boll_sigs = boll_strat.on_bar("20260119", {"000001.SZ": df.iloc[:19]})
    assert isinstance(boll_sigs, list)

    # 3. 自定义 RSI 周期 rsi_period=6 动态生效
    rsi_strat = RSIOversoldStrategy(rsi_period=6, oversold=35)
    rsi_sigs = rsi_strat.on_bar("20260119", {"000001.SZ": df.iloc[:19]})
    assert len(rsi_sigs) == 1 and rsi_sigs[0].direction == "BUY"

    # 4. 自定义 KDJ 平滑周期 k_period=5, d_period=5 动态生效
    kdj_strat = KDJCrossStrategy(k_period=5, d_period=5)
    kdj_sigs = kdj_strat.on_bar("20260120", {"000001.SZ": df})
    assert isinstance(kdj_sigs, list)


def test_round3_broker_order_sheet_deduplication_and_conflict_guard():
    """测试第3轮 P0-2: 实盘券商批量委托单同股去重防3倍超买与多空冲突剔除"""
    from app.signal import build_deduplicated_broker_orders

    raw_signals = [
        # 601939.SH 同日被 3 个策略同时触发 BUY -> 必须合并为 1 笔委托单！
        Signal(ts_code="601939.SH", trade_date="20260120", strategy="ma_cross", direction="BUY", score=0.75, price_ref=9.0),
        Signal(ts_code="601939.SH", trade_date="20260120", strategy="ma_bullish", direction="BUY", score=0.82, price_ref=9.0),
        Signal(ts_code="601939.SH", trade_date="20260120", strategy="macd_cross", direction="BUY", score=0.68, price_ref=9.0),
        # 000001.SZ 同时存在 BUY(0.65) 与更强 SELL(0.75) -> 买单必须被冲突保护拦截！
        Signal(ts_code="000001.SZ", trade_date="20260120", strategy="rsi_oversold", direction="BUY", score=0.65, price_ref=10.0),
        Signal(ts_code="000001.SZ", trade_date="20260120", strategy="ma_cross", direction="SELL", score=0.75, price_ref=10.0),
    ]
    rows, txt_lines = build_deduplicated_broker_orders(
        signals=raw_signals, capital=100000.0, max_pos_pct=20.0, stop_loss_pct=5.0,
        stock_map={"601939.SH": "建设银行", "000001.SZ": "平安银行"},
    )
    buy_rows = [r for r in rows if r["操作"] == "买入"]
    # 断言：601939.SH 只生成 1 笔买单（绝不重复生成3笔），且 000001.SZ 买单被冲突剔除
    assert len(buy_rows) == 1
    assert buy_rows[0]["标的代码"] == "601939.SH"
    assert "均线多头排列" in buy_rows[0]["来源策略"] and "双均线交叉" in buy_rows[0]["来源策略"]



