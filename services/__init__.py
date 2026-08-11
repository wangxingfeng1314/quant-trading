"""Service 层 - 封装业务逻辑，隔离 UI 与数据访问

使用方式:
    from services import DataService, BacktestService, SignalService

设计原则:
    1. UI 页面只调 Service，不直接调 storage/fetcher/scanner
    2. Service 层负责输入校验、业务编排、错误转换
    3. Service 层不依赖 Streamlit，可在脚本/定时任务中复用
"""
from services.data_service import DataService
from services.backtest_service import BacktestService
from services.signal_service import SignalService
from services.validators import (
    validate_stock_code, validate_date, validate_date_range,
    validate_capital, InvalidStockCodeError, InvalidDateError,
)

__all__ = [
    "DataService",
    "BacktestService",
    "SignalService",
    "validate_stock_code",
    "validate_date",
    "validate_date_range",
    "validate_capital",
    "InvalidStockCodeError",
    "InvalidDateError",
]
