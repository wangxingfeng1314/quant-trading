"""策略行情视图 —— 列式 numpy 支撑的只读 DataFrame 兼容子集

## 为什么需要它

pandas 3.0 移除了 ``DataFrame._item_cache``，``df[col]`` 不再复用已抽取的列，
每次访问都要重新构造一个 Series。在回测主循环里，每根 K 线、每只标的、
每个指标列都会触发一次这样的构造，实测单次 ``df["close"].iloc[-1]`` 约
24~45µs，而真正取值只需要几十纳秒 —— 这些开销占掉了策略耗时的大头。

本模块提供 ``BarFrame``：底层引用整段 numpy 数组，按「当前窗口长度」暴露
``df[col].iloc[-1]`` / ``df[col].iloc[-2]`` / ``df[col].iloc[-N:]`` 等尾部访问，
不做任何 Series 构造。

## 兼容性策略

``BarFrame`` 只实现了回测策略真正用到的那部分 DataFrame 接口：

- ``len(df)`` / ``df.columns``
- ``df[col]`` → ``ColumnView``（支持 ``.iloc[-1]`` / ``.iloc[-2]`` / 切片 / ``.rolling()``）
- ``df.iloc[:n]`` / ``df.iloc[:-1]`` → 新的 ``BarFrame``（仍是快路径）
- ``df.to_frame()`` → 真实 DataFrame 切片

**任何未实现的属性都会经 ``__getattr__`` 自动回退到真实 DataFrame**，
因此即便策略用了这里没覆盖的接口，行为仍然正确，只是退回慢路径，不会报错。
这条兜底是"可以放心交给第三方策略"的关键。
"""
from typing import Optional

import numpy as np
import pandas as pd

__all__ = ["BarFrame", "ColumnView", "FrameStore"]


class ColumnView:
    """单列的只读窗口视图，``.iloc`` 语义与 pandas Series 兼容。

    只覆盖尾部访问与切片；``rolling()`` 等需要完整序列的运算会退回真实 Series。
    """

    __slots__ = ("_arr", "_start", "_stop", "_name")

    def __init__(self, arr: np.ndarray, start: int, stop: int, name: str):
        self._arr = arr
        self._start = start
        self._stop = stop
        self._name = name

    # ---- 序列协议 ----
    def __len__(self) -> int:
        return max(0, self._stop - self._start)

    def __getitem__(self, key):
        if isinstance(key, slice):
            # 先截窗口再切，天然正确处理负索引/负步长
            window = self._arr[self._start:self._stop]
            s, e, st = key.indices(len(window))
            if st < 0 and e == -1:
                # 负步长时 indices() 用 -1 表示「一直取到开头之前」，
                # 直接传给切片会得到空数组，必须还原成 None
                e = None
            return pd.Series(window[s:e:st], name=self._name)
        i = int(key)
        if i < 0:
            i += len(self)
        if i < 0 or i >= len(self):
            raise IndexError("single positional indexer is out-of-bounds")
        return self._arr[self._start + i]

    def __array__(self, dtype=None, copy=None):
        arr = self.to_numpy()
        if dtype is not None:
            arr = arr.astype(dtype, copy=False)
        return arr

    def __iter__(self):
        return iter(self.to_numpy())

    def __repr__(self) -> str:
        return f"ColumnView({self._name!r}, len={len(self)})"

    # ---- pandas 兼容表面 ----
    @property
    def iloc(self) -> "ColumnView":
        # Series.iloc[-1] 与 ColumnView[-1] 等价，直接把 iloc 指回自身
        return self

    @property
    def loc(self):
        return self.to_series().loc

    @property
    def name(self) -> str:
        return self._name

    @property
    def dtype(self):
        return self._arr.dtype

    @property
    def values(self) -> np.ndarray:
        return self.to_numpy()

    @property
    def empty(self) -> bool:
        return len(self) == 0

    @property
    def index(self) -> pd.RangeIndex:
        return pd.RangeIndex(len(self))

    def to_numpy(self) -> np.ndarray:
        return self._arr[self._start:self._stop]

    def to_series(self) -> pd.Series:
        return pd.Series(self.to_numpy(), name=self._name)

    def rolling(self, window: int, min_periods: Optional[int] = None):
        return self.to_series().rolling(window=window, min_periods=min_periods)

    def ewm(self, **kwargs):
        return self.to_series().ewm(**kwargs)

    def shift(self, periods: int = 1):
        return self.to_series().shift(periods)

    def __getattr__(self, item):
        # 未覆盖的 Series 方法：退回真实 Series（慢但正确）
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return getattr(self.to_series(), item)


class FrameStore:
    """单只标的的列容器，整个回测期间复用。

    列数组按需惰性物化：只有策略真正读到的列才会转成 numpy，
    避免为 30 列全部建数组（全市场扫描时这是实打实的内存开销）。
    float64 列调用 ``to_numpy()`` 返回的是底层视图，不复制。
    """

    __slots__ = ("frame", "columns", "n", "_arrays")

    def __init__(self, df: pd.DataFrame):
        self.frame = df
        self.columns = df.columns
        self.n = len(df)
        self._arrays: dict = {}

    def array(self, col: str) -> np.ndarray:
        arr = self._arrays.get(col)
        if arr is None:
            arr = self.frame[col].to_numpy()
            self._arrays[col] = arr
        return arr

    def column(self, col: str, start: int, stop: int) -> ColumnView:
        return ColumnView(self.array(col), start, stop, col)


class _ILoc:
    """``BarFrame.iloc`` 的整数/切片访问器"""

    __slots__ = ("_f",)

    def __init__(self, frame: "BarFrame"):
        self._f = frame

    def __getitem__(self, key):
        f = self._f
        if isinstance(key, slice):
            s, e, st = key.indices(len(f))
            if st != 1:
                # 非常规步长：退回真实 DataFrame，保证语义绝对正确
                return f.to_frame().iloc[s:e:st]
            return BarFrame(f._store, f._start + s, f._start + e)
        # 单行访问（df.iloc[-1]）需要 30 列整行，直接走真实 DataFrame
        return f.to_frame().iloc[key]

    def __len__(self) -> int:
        return len(self._f)


class BarFrame:
    """回测中传给策略的行情视图：``{ts_code: BarFrame}``

    语义等价于 ``df.iloc[:idx + 1]``，但列访问走 numpy 零构造快路径。
    """

    __slots__ = ("_store", "_start", "_stop")

    def __init__(self, store: FrameStore, start: int = 0, stop: Optional[int] = None):
        self._store = store
        self._start = start
        self._stop = store.n if stop is None else stop

    # ---- 基本协议 ----
    def __len__(self) -> int:
        return max(0, self._stop - self._start)

    def __getitem__(self, key):
        if isinstance(key, (list, tuple, pd.Index, np.ndarray)):
            return self.to_frame()[key]
        if key not in self._store._arrays and key not in self._store.columns:
            raise KeyError(key)
        return self._store.column(key, self._start, self._stop)

    def __contains__(self, key) -> bool:
        return key in self._store.columns

    def __iter__(self):
        return iter(self._store.columns)

    def __repr__(self) -> str:
        return (f"BarFrame(rows={len(self)}, "
                f"cols={len(self._store.columns)}, "
                f"window=[{self._start}:{self._stop}])")

    # ---- pandas 兼容表面 ----
    @property
    def columns(self):
        return self._store.columns

    @property
    def iloc(self) -> _ILoc:
        return _ILoc(self)

    @property
    def empty(self) -> bool:
        return len(self) == 0

    @property
    def shape(self):
        return (len(self), len(self._store.columns))

    @property
    def index(self):
        return pd.RangeIndex(self._start, self._stop)

    def to_frame(self) -> pd.DataFrame:
        """物化为真实 DataFrame 切片（不在快路径时使用）"""
        return self._store.frame.iloc[self._start:self._stop]

    def copy(self) -> pd.DataFrame:
        return self.to_frame().copy()

    def __getattr__(self, item):
        # 兜底：任何未实现的 DataFrame 接口退回真实 DataFrame，保证正确性。
        # 内部槽位与 dunder 必须走正常失败路径，否则 __init__ 之前访问会无限递归。
        if item in ("_store", "_start", "_stop") or (item.startswith("__") and item.endswith("__")):
            raise AttributeError(item)
        return getattr(self.to_frame(), item)
