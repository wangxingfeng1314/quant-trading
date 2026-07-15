"""持仓管理 - 单只股票的持仓跟踪"""
from dataclasses import dataclass, field


@dataclass
class Position:
    """单只股票持仓"""
    ts_code: str
    shares: int = 0
    avg_cost: float = 0.0  # 摊薄成本价（含手续费）
    buy_date: str = ""  # 最近一次买入日期（用于T+1判断）
    total_cost: float = 0.0  # 累计投入成本

    @property
    def is_empty(self) -> bool:
        return self.shares <= 0

    def can_sell(self, trade_date: str) -> bool:
        """T+1规则：买入当日不可卖出"""
        if self.is_empty:
            return False
        return trade_date > self.buy_date

    def buy(self, price: float, volume: int, cost: float, trade_date: str):
        """加仓"""
        new_cost = self.avg_cost * self.shares + price * volume + cost
        self.shares += volume
        self.avg_cost = new_cost / self.shares if self.shares > 0 else 0
        self.total_cost += price * volume + cost
        self.buy_date = trade_date

    def sell(self, volume: int, price: float, cost: float) -> float:
        """减仓，返回已实现盈亏"""
        if volume > self.shares:
            volume = self.shares
        realized_pnl = (price - self.avg_cost) * volume - cost
        self.shares -= volume
        if self.shares <= 0:
            self.shares = 0
            self.avg_cost = 0.0
            self.total_cost = 0.0
        return round(realized_pnl, 2)

    def market_value(self, current_price: float) -> float:
        """当前市值"""
        return self.shares * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        """浮动盈亏"""
        return (current_price - self.avg_cost) * self.shares

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """浮动盈亏百分比"""
        if self.avg_cost <= 0:
            return 0.0
        return (current_price / self.avg_cost - 1) * 100
