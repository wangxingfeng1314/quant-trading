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
    available_shares: int = 0  # 当日可卖股数 (T+1)
    frozen_shares: int = 0  # 当日买入冻结股数
    current_date: str = ""  # 当前交易日

    @property
    def is_empty(self) -> bool:
        return self.shares <= 0

    def on_new_day(self, trade_date: str):
        """进入新交易日，昨日冻结份额转为可用"""
        self.current_date = trade_date
        self.available_shares = self.shares
        self.frozen_shares = 0

    def can_sell(self, trade_date: str, volume: int = 1) -> bool:
        """T+1规则：买入当日不可卖出，只可卖出可用份额"""
        if self.is_empty:
            return False
        # 跨日自动结转（兼容未显式调用 on_new_day 的场景）
        if self.current_date != trade_date:
            if not self.buy_date or trade_date > self.buy_date:
                self.on_new_day(trade_date)
            else:
                self.current_date = trade_date
        return self.available_shares >= volume and self.available_shares > 0

    def buy(self, price: float, volume: int, cost: float, trade_date: str):
        """加仓（T+1冻结当日买入份额）"""
        if self.current_date != trade_date:
            if not self.buy_date or trade_date > self.buy_date:
                self.on_new_day(trade_date)
            else:
                self.current_date = trade_date

        new_cost = self.avg_cost * self.shares + price * volume + cost
        self.shares += volume
        self.frozen_shares += volume
        self.avg_cost = new_cost / self.shares if self.shares > 0 else 0
        self.total_cost += price * volume + cost
        self.buy_date = trade_date

    def sell(self, volume: int, price: float, cost: float) -> float:
        """减仓，优先校验可用份额，返回已实现盈亏"""
        max_sell = self.available_shares if self.available_shares > 0 else self.shares
        if volume > max_sell:
            volume = max_sell
        if volume <= 0:
            return 0.0

        realized_pnl = (price - self.avg_cost) * volume - cost
        self.shares -= volume
        self.available_shares = max(0, self.available_shares - volume)
        if self.shares <= 0:
            self.shares = 0
            self.available_shares = 0
            self.frozen_shares = 0
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
