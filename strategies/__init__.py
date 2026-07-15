"""策略注册表"""
from strategies.ma_cross import MACrossStrategy
from strategies.macd_divergence import MACDDivergenceStrategy
from strategies.turtle import TurtleStrategy
from strategies.rsi_oversold import RSIOversoldStrategy
from strategies.bollinger_reversal import BollingerReversalStrategy
from strategies.kdj_cross import KDJCrossStrategy
from strategies.ma_bullish import MABullishStrategy
from strategies.donchian_breakout import DonchianBreakoutStrategy
from strategies.volume_price_breakout import VolumePriceBreakoutStrategy
from strategies.double_bottom import DoubleBottomStrategy
from strategies.multi_factor import MultiFactorStrategy

# 策略注册表：名称 -> 策略类
# 添加新策略只需在这里加一行
STRATEGY_REGISTRY = {
    "ma_cross": MACrossStrategy,
    "macd_divergence": MACDDivergenceStrategy,
    "turtle": TurtleStrategy,
    "rsi_oversold": RSIOversoldStrategy,
    "bollinger_reversal": BollingerReversalStrategy,
    "kdj_cross": KDJCrossStrategy,
    "ma_bullish": MABullishStrategy,
    # ===== 新增策略 =====
    "donchian_breakout": DonchianBreakoutStrategy,         # 趋势跟踪 - 唐奇安通道突破
    "volume_price_breakout": VolumePriceBreakoutStrategy,   # 动量 - 量价突破
    "double_bottom": DoubleBottomStrategy,                  # 反转 - 双底形态识别
    "multi_factor": MultiFactorStrategy,                    # 组合 - 多因子综合评分
}


def get_strategy(name: str):
    """根据名称获取策略类"""
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"未知策略: {name}, 可选: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> list:
    """列出所有策略"""
    return [
        {"name": name, "desc": cls.description, "params": cls.param_schema}
        for name, cls in STRATEGY_REGISTRY.items()
    ]
