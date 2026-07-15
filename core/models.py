"""核心数据模型"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Signal:
    """交易信号"""
    ts_code: str
    trade_date: str  # YYYYMMDD
    strategy: str
    direction: str  # 'BUY' or 'SELL'
    score: float = 0.0  # 策略置信度 0-1
    reason: str = ""
    price_ref: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Trade:
    """交易记录"""
    ts_code: str
    direction: str  # 'BUY' or 'SELL'
    trade_date: str
    price: float
    volume: int
    commission: float = 0.0
    tax: float = 0.0
    pnl: float = 0.0  # 已实现盈亏（仅卖出时）
    holding_days: int = 0  # 持仓天数（仅卖出时）


@dataclass
class PositionInfo:
    """持仓信息"""
    ts_code: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    buy_date: str = ""
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class BacktestResult:
    """回测结果"""
    strategy: str
    params: str  # JSON字符串
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float = 0.0  # 百分比
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    equity_curve: list = field(default_factory=list)  # [{date, equity}, ...]
    trades: list = field(default_factory=list)  # Trade列表


@dataclass
class StockInfo:
    """股票基本信息"""
    ts_code: str
    symbol: str
    name: str
    market: str  # 'SH' or 'SZ'
    list_date: str = ""
    delist_date: str = ""
    industry: str = ""
    is_st: bool = False
