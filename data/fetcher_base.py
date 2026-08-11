"""数据源抽象基类 - 定义统一的数据源接口

所有数据源（AKShare/TickFlow/Tushare/Baostock）应实现此接口。
新增数据源只需：
  1. 继承 DataSource
  2. 实现所有 abstractmethod
  3. 在 fetcher.py 的级联列表中注册
"""
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd


# 日线数据统一列定义（所有数据源输出格式一致）
DAILY_COLUMNS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "volume", "amount", "pct_chg", "turnover", "adj_factor",
]


class DataSource(ABC):
    """数据源抽象基类

    子类必须实现:
        - name: 数据源名称
        - fetch_daily(): 获取日线数据
        - fetch_stock_list(): 获取股票列表（可选，默认返回空）
        - fetch_index_daily(): 获取指数日线（可选，默认返回空）

    可选实现:
        - fetch_etf_list(): 获取 ETF 列表
        - fetch_index_constituents(): 获取指数成分股
    """

    name: str = "base"
    requires_token: bool = False  # 是否需要 API Key/Token

    @abstractmethod
    def fetch_daily(self, ts_code: str, start_date: str = "",
                    end_date: str = "", days: int = 0) -> Optional[pd.DataFrame]:
        """获取日线数据

        Args:
            ts_code: 股票代码
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            days: 获取最近 N 天（0=不限）

        Returns:
            包含 DAILY_COLUMNS 列的 DataFrame，失败返回 None
        """
        ...

    def fetch_stock_list(self) -> Optional[pd.DataFrame]:
        """获取股票列表（默认返回 None，子类按需实现）"""
        return None

    def fetch_etf_list(self) -> Optional[pd.DataFrame]:
        """获取 ETF 列表（默认返回 None）"""
        return None

    def fetch_index_daily(self, ts_code: str,
                          limit: int = 0) -> Optional[pd.DataFrame]:
        """获取指数日线数据（默认返回 None）"""
        return None

    def fetch_index_constituents(self, index_code: str) -> Optional[list]:
        """获取指数成分股列表（默认返回 None）"""
        return None

    def is_available(self) -> bool:
        """检查数据源是否可用（默认 True，需要 Token 的源需重写此方法）"""
        return True
