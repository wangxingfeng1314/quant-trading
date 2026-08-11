"""A股量化交易系统 - 核心配置和模型"""

# 导出自定义异常，方便外部使用
from core.exceptions import (
    QuantError,
    DataFetchError,
    SourceUnavailableError,
    DataFormatError,
    BacktestError,
    InsufficientDataError,
    InvalidParamsError,
    ValidationError,
    InvalidStockCodeError,
    InvalidDateError,
    StorageError,
    DatabaseLockError,
)
