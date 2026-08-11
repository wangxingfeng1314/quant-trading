"""策略基类"""
from abc import ABC, abstractmethod
from typing import List
from core.models import Signal


class BaseStrategy(ABC):
    """所有策略的基类

    子类必须实现:
        - name: 类属性，策略名称
        - on_bar(): 每个交易日调用，返回信号列表

    可选类属性:
        - style: 策略风格分类（短线/震荡/中长线/综合），用于页面分组
    """
    name: str = "base"
    description: str = ""
    style: str = "综合"  # 策略风格: 短线 / 震荡 / 中长线 / 综合
    param_schema: dict = {}  # {参数名: {'default': 默认值, 'desc': '说明'}}

    @abstractmethod
    def on_bar(self, trade_date: str, data: dict, portfolio=None) -> List[Signal]:
        """每个交易日调用

        Args:
            trade_date: 当前日期 'YYYYMMDD'
            data: {ts_code: DataFrame} 每只股票从开始到当日的数据（含指标列）
            portfolio: Portfolio对象，可查看当前持仓

        Returns:
            Signal列表，每个Signal包含 ts_code, direction, score, reason, price_ref
        """
        return []
