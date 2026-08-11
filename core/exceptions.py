"""自定义异常层次 - 统一错误处理

所有模块应优先使用这些异常而非通用 Exception/ValueError，
以便上层按异常类型做精细化错误处理和用户提示。

异常层次:
    QuantError                    # 所有自定义异常的基类
    ├── DataFetchError            # 数据获取失败
    │   ├── SourceUnavailableError   # 数据源不可用（网络/认证）
    │   └── DataFormatError         # 数据格式异常
    ├── BacktestError             # 回测引擎错误
    │   ├── InsufficientDataError   # 数据不足
    │   └── InvalidParamsError      # 参数无效
    ├── ValidationError           # 输入校验失败
    │   ├── InvalidStockCodeError   # 股票代码格式错误
    │   └── InvalidDateError        # 日期格式错误
    └── StorageError              # 存储层错误
        └── DatabaseLockError       # 数据库锁定
"""


class QuantError(Exception):
    """所有自定义异常的基类"""


# ============================================================
# 数据获取相关
# ============================================================

class DataFetchError(QuantError):
    """数据获取失败"""

    def __init__(self, message: str, source: str = ""):
        super().__init__(message)
        self.source = source


class SourceUnavailableError(DataFetchError):
    """数据源不可用（网络异常/认证失败/熔断）"""


class DataFormatError(DataFetchError):
    """数据格式异常（列缺失/类型错误/空数据）"""


# ============================================================
# 回测引擎相关
# ============================================================

class BacktestError(QuantError):
    """回测引擎错误"""


class InsufficientDataError(BacktestError):
    """数据不足，无法完成回测"""

    def __init__(self, message: str, available: int = 0, required: int = 0):
        super().__init__(message)
        self.available = available
        self.required = required


class InvalidParamsError(BacktestError):
    """回测参数无效"""

    def __init__(self, message: str, param_name: str = ""):
        super().__init__(message)
        self.param_name = param_name


# ============================================================
# 输入校验相关
# ============================================================

class ValidationError(QuantError):
    """输入校验失败"""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


class InvalidStockCodeError(ValidationError):
    """股票代码格式错误"""

    def __init__(self, code: str):
        super().__init__(f"无效的股票代码: {code}（格式应为 6位数字.SH/SZ/BJ）", field="ts_code")
        self.code = code


class InvalidDateError(ValidationError):
    """日期格式错误"""

    def __init__(self, date_str: str):
        super().__init__(f"无效的日期格式: {date_str}（格式应为 YYYYMMDD）", field="date")
        self.date_str = date_str


# ============================================================
# 存储层相关
# ============================================================

class StorageError(QuantError):
    """存储层错误"""


class DatabaseLockError(StorageError):
    """数据库锁定（可能被其他进程占用）"""
