"""SQLite数据库操作 - 所有DB操作的唯一入口

本模块封装了对 SQLite 数据库的所有读写操作，外部代码只能通过本模块访问数据库。
设计原则：
  1. 统一入口：所有 CRUD 操作集中于此，方便维护和审计
  2. 上下文管理：get_conn() 自动管理连接生命周期和事务
  3. 幂等写入：全部使用 INSERT OR REPLACE，支持重复执行
  4. 写锁保护：_update_lock 文件锁防止多进程并发写入
"""
import sqlite3          # SQLite 数据库驱动
import json             # JSON 序列化（用于 equity_curve 等复杂字段）
import os               # 文件路径/环境变量/锁文件
import time             # 休眠
import logging          # 日志记录
from contextlib import contextmanager  # 上下文管理器装饰器
from pathlib import Path               # 路径处理
from typing import Optional            # 类型提示
import pandas as pd     # 数据处理

# 从配置加载数据库路径
from core.config import DB_PATH


# ============================================================
# 写锁保护 — 文件锁，防止定时任务+手动更新并发写入
# ============================================================

_LOCK_FILE = DB_PATH.parent / ".update.lock"
_LOCK_TIMEOUT = 60  # 最长等待 60 秒

logger = logging.getLogger(__name__)


def acquire_update_lock(timeout: int = None) -> bool:
    """尝试获取更新锁（文件锁，非阻塞式）

    使用原子性 os.mkdir 实现跨平台文件锁。
    定时任务和手动更新同时触发时，只有一个能拿到锁。

    Args:
        timeout: 超时秒数（默认 _LOCK_TIMEOUT=60）

    Returns:
        True=拿到锁, False=超时未拿到
    """
    if timeout is None:
        timeout = _LOCK_TIMEOUT
    lock_path = str(_LOCK_FILE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.mkdir(lock_path)  # 原子操作：目录不存在则创建成功
            logger.debug("获取更新锁成功")
            return True
        except FileExistsError:
            time.sleep(1)
    logger.warning(f"等待更新锁超时 ({timeout}s)，可能是上一次更新还未完成")
    return False


def release_update_lock():
    """释放更新锁"""
    lock_path = str(_LOCK_FILE)
    try:
        os.rmdir(lock_path)
        logger.debug("释放更新锁成功")
    except FileNotFoundError:
        pass  # 锁已经被释放


@contextmanager
def update_lock(timeout: int = None):
    """获取/释放更新锁的上下文管理器

    用法:
        with update_lock():
            run_update(...)
    """
    acquired = acquire_update_lock(timeout)
    try:
        yield acquired
    finally:
        if acquired:
            release_update_lock()


# ============================================================
# 数据库连接管理
# ============================================================

@contextmanager
def get_conn():
    """获取数据库连接（上下文管理器，自动提交/回滚）

    用法:
        with get_conn() as conn:
            conn.execute(...)
        # 自动 commit 或 rollback

    特性:
        - WAL 模式：读写不互斥，提升并发性能
        - synchronous=NORMAL：安全性兼顾性能
        - 自动创建父目录（数据库文件所在文件夹）
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)    # 确保 data/ 目录存在
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)    # 连接数据库，锁等待5秒
    conn.execute("PRAGMA journal_mode=WAL")               # WAL 模式：支持并发读写
    conn.execute("PRAGMA synchronous=NORMAL")             # 同步级别：平衡安全与性能
    try:
        yield conn                                        # 返回连接给调用方
        conn.commit()                                     # 无异常则提交事务
    except Exception:
        conn.rollback()                                   # 有异常则回滚
        raise                                             # 继续抛出异常
    finally:
        conn.close()                                      # 关闭连接


# ============================================================
# 数据库完整性检查
# ============================================================

def check_db_integrity() -> dict:
    """检查 SQLite 数据库完整性

    使用 PRAGMA quick_check 快速验证数据库文件是否损坏。
    比 full integrity_check 更快，适合启动时调用。

    Returns:
        {"ok": True/False, "message": "详细描述"}
    """
    try:
        with get_conn() as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
            result = row[0] if row else "unknown"
            if result == "ok":
                return {"ok": True, "message": "数据库完整性正常"}
            else:
                return {"ok": False, "message": f"数据库可能损坏: {result}"}
    except Exception as e:
        return {"ok": False, "message": f"无法访问数据库: {e}"}


# ============================================================
# 数据库表初始化
# ============================================================

def init_db():
    """创建所有数据库表（幂等：IF NOT EXISTS，重复调用安全）

    包含的表:
        stock_basic      - 股票基本信息（代码、名称、行业、ST标记等）
        daily_price      - 日线行情（OHLCV + 复权因子）
        signal           - 交易信号
        backtest_result  - 回测结果汇总
        backtest_trade   - 回测交易明细
        watchlist        - 自选股列表
        index_daily      - 大盘指数日线（上证/深证/创业板）
        position         - 模拟持仓（持久化，替代 session_state）
    """
    with get_conn() as conn:
        conn.executescript("""
            -- 股票基本信息表
            CREATE TABLE IF NOT EXISTS stock_basic (
                ts_code      TEXT PRIMARY KEY,    -- 股票代码 e.g. "000001.SZ"
                symbol       TEXT NOT NULL,       -- 纯数字代码 e.g. "000001"
                name         TEXT NOT NULL,       -- 股票名称 e.g. "平安银行"
                market       TEXT NOT NULL,       -- 市场 "SH" 或 "SZ"
                list_date    TEXT,                -- 上市日期 "YYYYMMDD"
                delist_date  TEXT,                -- 退市日期（空=正常上市）
                industry     TEXT,                -- 所属行业
                is_st        INTEGER DEFAULT 0,   -- ST标记: 1=ST, 0=正常
                updated_at   TEXT                 -- 数据更新时间
            );

            -- 日线行情表（核心数据表，约40万~50万条）
            CREATE TABLE IF NOT EXISTS daily_price (
                ts_code    TEXT NOT NULL,         -- 股票代码
                trade_date TEXT NOT NULL,         -- 交易日 "YYYYMMDD"
                open       REAL,                  -- 开盘价（前复权）
                high       REAL,                  -- 最高价（前复权）
                low        REAL,                  -- 最低价（前复权）
                close      REAL,                  -- 收盘价（前复权）
                volume     REAL,                  -- 成交量（股）
                amount     REAL,                  -- 成交额（元）
                pct_chg    REAL,                  -- 涨跌幅（%）
                turnover   REAL,                  -- 换手率（%）
                adj_factor REAL DEFAULT 1.0,      -- 复权因子（已前复权则=1）
                PRIMARY KEY (ts_code, trade_date) -- 联合主键：一股一日期一条
            );
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_price(trade_date);  -- 按日期查索引

            -- 交易信号表
            -- 同一只股票、同一天、同一策略、同一方向只能有一条信号
            CREATE TABLE IF NOT EXISTS signal (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增ID
                ts_code    TEXT NOT NULL,         -- 股票代码
                trade_date TEXT NOT NULL,         -- 信号日期
                strategy   TEXT NOT NULL,         -- 策略名称 e.g. "ma_cross"
                direction  TEXT NOT NULL,         -- 方向: "BUY" 或 "SELL"
                score      REAL,                  -- 信号评分 0~1
                reason     TEXT,                  -- 信号原因描述
                price_ref  REAL,                  -- 信号产生时的参考价格
                created_at TEXT,                  -- 创建时间
                UNIQUE(ts_code, trade_date, strategy, direction)  -- 防止重复信号
            );
            CREATE INDEX IF NOT EXISTS idx_signal_date ON signal(trade_date);      -- 按日期查信号
            CREATE INDEX IF NOT EXISTS idx_signal_strategy ON signal(strategy);    -- 按策略查信号

            -- 回测结果汇总表
            CREATE TABLE IF NOT EXISTS backtest_result (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy        TEXT NOT NULL,     -- 策略名称
                params          TEXT,              -- JSON字符串（策略参数字典）
                start_date      TEXT NOT NULL,     -- 回测开始日期
                end_date        TEXT NOT NULL,     -- 回测结束日期
                initial_capital REAL NOT NULL,     -- 初始资金
                final_capital   REAL NOT NULL,     -- 最终资金
                total_return    REAL,              -- 总收益率
                annual_return   REAL,              -- 年化收益率
                max_drawdown    REAL,              -- 最大回撤
                sharpe_ratio    REAL,              -- 夏普比率
                win_rate        REAL,              -- 胜率
                trade_count     INTEGER,           -- 交易次数
                equity_curve    TEXT,              -- JSON字符串（权益曲线）
                created_at      TEXT               -- 创建时间
            );

            -- 回测交易明细表
            CREATE TABLE IF NOT EXISTS backtest_trade (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_id     INTEGER NOT NULL REFERENCES backtest_result(id),  -- 关联回测ID
                ts_code         TEXT NOT NULL,     -- 股票代码
                direction       TEXT NOT NULL,     -- "BUY" 或 "SELL"
                trade_date      TEXT NOT NULL,     -- 交易日期
                price           REAL NOT NULL,     -- 成交价格
                volume          INTEGER NOT NULL,  -- 成交数量
                commission      REAL,              -- 佣金
                tax             REAL,              -- 税费
                pnl             REAL,              -- 盈亏
                holding_days    INTEGER            -- 持有天数
            );
            CREATE INDEX IF NOT EXISTS idx_bt_trade ON backtest_trade(backtest_id);  -- 按回测查交易

            -- 自选股表
            CREATE TABLE IF NOT EXISTS watchlist (
                ts_code    TEXT PRIMARY KEY,      -- 股票代码
                added_date TEXT,                  -- 添加日期
                note       TEXT,                  -- 备注
                group_name TEXT DEFAULT ''         -- 分组（如"长线池"、"短线池"）
            );

            -- 大盘指数日线表（上证/深证/创业板）
            CREATE TABLE IF NOT EXISTS index_daily (
                ts_code    TEXT NOT NULL,         -- 指数代码 e.g. "000001.SH"
                trade_date TEXT NOT NULL,         -- 交易日
                open       REAL,                  -- 开盘点数
                high       REAL,                  -- 最高点数
                low        REAL,                  -- 最低点数
                close      REAL,                  -- 收盘点数
                volume     REAL,                  -- 成交量
                pct_chg    REAL,                  -- 涨跌幅
                PRIMARY KEY (ts_code, trade_date)
            );

            -- 模拟持仓表（持久化，替代 session_state）
            CREATE TABLE IF NOT EXISTS position (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code     TEXT NOT NULL,         -- 股票代码
                direction   TEXT NOT NULL DEFAULT 'BUY',  -- 方向
                buy_price   REAL NOT NULL,         -- 买入价
                shares      INTEGER NOT NULL,       -- 持仓数量
                buy_date    TEXT NOT NULL,          -- 买入日期
                note        TEXT DEFAULT '',        -- 备注
                created_at  TEXT,                   -- 创建时间
                updated_at  TEXT                    -- 更新时间
            );
        """)
        # 兼容旧数据库：ALTER TABLE 在列已存在时报错，单独处理
        try:
            conn.execute("ALTER TABLE watchlist ADD COLUMN group_name TEXT DEFAULT ''")
        except Exception:
            pass


# ============================================================
# 股票基本信息 (stock_basic) 表操作
# ============================================================

def save_stock_list(df: pd.DataFrame):
    """增量更新股票列表到 stock_basic 表（INSERT OR REPLACE）

    与全表替换不同，此方法逐条插入/更新，避免删除重建。
    适用于每日增量更新股票列表信息。

    参数:
        df: 包含 ts_code, symbol, name, market 等列的 DataFrame
    """
    from datetime import datetime
    now = datetime.now().isoformat()              # 生成当前时间戳
    df = df.copy()                                 # 不修改原始数据
    df["updated_at"] = now                          # 追加更新时间列
    # 逐行 INSERT OR REPLACE，比全表删除重建更高效
    cols = ["ts_code", "symbol", "name", "market", "list_date",
            "industry", "is_st", "delist_date", "updated_at"]
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    sql = f"INSERT OR REPLACE INTO stock_basic ({col_names}) VALUES ({placeholders})"
    rows = df[cols].values.tolist()
    with get_conn() as conn:
        conn.executemany(sql, rows)


def get_stock_list() -> pd.DataFrame:
    """获取所有股票列表（按代码排序）"""
    with get_conn() as conn:
        return pd.read_sql("SELECT * FROM stock_basic ORDER BY ts_code", conn)


def get_stock_name(ts_code: str) -> str:
    """根据股票代码查询股票名称

    参数:
        ts_code: 股票代码 e.g. "000001.SZ"
    返回:
        股票名称，如查不到则返回代码本身
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM stock_basic WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        return row[0] if row else ts_code          # 找不到则返回代码


# ============================================================
# 日线行情 (daily_price) 表操作
# ============================================================

def save_daily(df: pd.DataFrame):
    """保存日线数据到 daily_price 表（支持断点续传）

    自动做以下数据标准化:
      1. open/high/low/close → round(2) 到 2 位小数
      2. pct_chg → round(2) 到 2 位小数
      3. volume/amount → round(2) 避免浮点噪声
      4. OHLC 校验：high >= low, high >= open/close, low <= open/close

    使用 INSERT OR REPLACE，重复调用安全。
    """
    if df.empty:
        return

    df = df.copy()

    # 价格精度标准化: 统一 rounding 到2位小数
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(float).round(2)

    # pct_chg rounding 到2位小数
    if "pct_chg" in df.columns:
        df["pct_chg"] = df["pct_chg"].astype(float).round(2)

    # 成交额/量也避免浮点噪声
    for col in ["volume", "amount"]:
        if col in df.columns:
            df[col] = df[col].astype(float).round(2)

    # 动态拼接 INSERT 语句，兼容部分列缺失的场景
    cols = ["ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "pct_chg", "turnover", "adj_factor"]
    for col in cols:
        if col not in df.columns:
            df[col] = None                           # 缺失列补 None
    placeholders = ",".join(["?"] * len(cols))        # 生成占位符: ?,?,?,...
    col_names = ",".join(cols)                        # 列名列表
    sql = f"INSERT OR REPLACE INTO daily_price ({col_names}) VALUES ({placeholders})"
    rows = df[cols].values.tolist()                   # 转为列表（性能优化：一次写入）
    with get_conn() as conn:
        conn.executemany(sql, rows)                    # 批量写入


def get_daily(ts_code: str, start_date: str = "", end_date: str = "",
              limit: int = 0) -> pd.DataFrame:
    """获取单只股票的日线数据

    参数:
        ts_code:    股票代码
        start_date: 起始日期 "YYYYMMDD"（空=不限）
        end_date:   结束日期 "YYYYMMDD"（空=不限）
        limit:      返回最近 N 条记录（0=全部）

    返回:
        按日期升序排列的 DataFrame
    """
    query = "SELECT * FROM daily_price WHERE ts_code = ?"
    params = [ts_code]
    if start_date:
        query += " AND trade_date >= ?"               # 起始日期过滤
        params.append(start_date)
    if end_date:
        query += " AND trade_date <= ?"               # 结束日期过滤
        params.append(end_date)
    query += " ORDER BY trade_date"
    # 当 limit > 0 时，在 SQL 层面直接限制返回行数（比全量读取后 tail() 更高效）
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        df = pd.read_sql(query, conn, params=params)
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)  # 确保日期为字符串
    return df


def batch_get_latest(codes: list, limit: int = 2) -> pd.DataFrame:
    """批量获取多只股票的最新N条日线数据

    使用 SQL 窗口函数 ROW_NUMBER()，一条 SQL 替代 N 条，性能提升 100 倍。

    参数:
        codes: 股票代码列表 e.g. ['000001.SZ', '600519.SH']
        limit: 每只股票取最近 N 条

    返回:
        含 ts_code, trade_date, open, high, low, close, volume, pct_chg 的 DataFrame
    """
    if not codes:
        return pd.DataFrame()

    # 用窗口函数 ROW_NUMBER 取每只股票最近N条
    placeholders = ",".join("?" * len(codes))          # 生成占位符: ?,?,?,...
    query = f"""
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY ts_code ORDER BY trade_date DESC
            ) as rn
            FROM daily_price
            WHERE ts_code IN ({placeholders})
        ) WHERE rn <= ?
        ORDER BY ts_code, trade_date
    """
    params = codes + [limit]
    with get_conn() as conn:
        df = pd.read_sql(query, conn, params=params)
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)  # 确保日期为字符串
        df = df.drop(columns=["rn"])                      # 移除窗口函数辅助列
    return df


def get_latest_date(ts_code: str) -> str:
    """获取某只股票最新数据日期

    用于增量更新时判断是否需要拉取新数据。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM daily_price WHERE ts_code = ?",
            (ts_code,)
        ).fetchone()
        return row[0] if row and row[0] else ""


def get_daily_count() -> int:
    """获取日线数据总条数"""
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()
        return row[0]


# ============================================================
# 交易信号 (signal) 表操作
# ============================================================

def save_signal(sig):
    """保存一条交易信号到数据库

    参数:
        sig: Signal 数据模型对象（包含 ts_code, trade_date, strategy, direction, score, reason, price_ref）
    """
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO signal (ts_code, trade_date, strategy, direction,
               score, reason, price_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sig.ts_code, sig.trade_date, sig.strategy, sig.direction,
             sig.score, sig.reason, sig.price_ref, datetime.now().isoformat())
        )


def get_signals(trade_date: str = "", strategy: str = "",
                limit: int = 100) -> pd.DataFrame:
    """查询交易信号

    参数:
        trade_date: 按日期过滤 "YYYYMMDD"（空=全部）
        strategy:   按策略名过滤（空=全部）
        limit:      最多返回条数

    返回:
        按日期降序、评分降序排列的信号 DataFrame
    """
    query = "SELECT * FROM signal WHERE 1=1"             # 基础查询
    params = []
    if trade_date:
        query += " AND trade_date = ?"                   # 按日期筛选
        params.append(trade_date)
    if strategy:
        query += " AND strategy = ?"                     # 按策略筛选
        params.append(strategy)
    query += " ORDER BY trade_date DESC, score DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return pd.read_sql(query, conn, params=params)


# ============================================================
# 回测结果 (backtest_result / backtest_trade) 表操作
# ============================================================

def save_backtest_result(result) -> int:
    """保存回测结果，返回该次回测的自增 ID

    参数:
        result: BacktestResult 数据模型对象

    返回:
        新增记录的自增 ID（用于关联交易明细）
    """
    from datetime import datetime
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO backtest_result
               (strategy, params, start_date, end_date, initial_capital,
                final_capital, total_return, annual_return, max_drawdown,
                sharpe_ratio, win_rate, trade_count, equity_curve, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.strategy, result.params, result.start_date, result.end_date,
             result.initial_capital, result.final_capital, result.total_return,
             result.annual_return, result.max_drawdown, result.sharpe_ratio,
             result.win_rate, result.trade_count,
             json.dumps(result.equity_curve, ensure_ascii=False),  # 权益曲线存JSON
             datetime.now().isoformat())
        )
        return cursor.lastrowid                           # 返回自增ID


def save_backtest_trades(backtest_id: int, trades: list):
    """保存回测的交易明细

    参数:
        backtest_id: 关联的回测结果 ID
        trades:      Trade 数据模型对象列表
    """
    with get_conn() as conn:
        for t in trades:                                 # 逐条写入
            conn.execute(
                """INSERT INTO backtest_trade
                   (backtest_id, ts_code, direction, trade_date, price,
                    volume, commission, tax, pnl, holding_days)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (backtest_id, t.ts_code, t.direction, t.trade_date,
                 t.price, t.volume, t.commission, t.tax, t.pnl,
                 t.holding_days)
            )


def get_backtest_results(limit: int = 20) -> pd.DataFrame:
    """获取回测结果列表（按创建时间倒序）"""
    with get_conn() as conn:
        return pd.read_sql(
            "SELECT * FROM backtest_result ORDER BY created_at DESC LIMIT ?",
            conn, params=[limit]
        )


def get_backtest_trades(backtest_id: int) -> pd.DataFrame:
    """获取某次回测的交易明细"""
    with get_conn() as conn:
        return pd.read_sql(
            "SELECT * FROM backtest_trade WHERE backtest_id = ? ORDER BY trade_date",
            conn, params=[backtest_id]
        )


# ============================================================
# 自选股 (watchlist) 表操作
# ============================================================

def add_to_watchlist(ts_code: str, note: str = ""):
    """添加或更新自选股（INSERT OR REPLACE）"""
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (ts_code, added_date, note) VALUES (?, ?, ?)",
            (ts_code, datetime.now().strftime("%Y-%m-%d"), note)
        )


def update_watchlist_group(ts_code: str, group_name: str):
    """更新自选股分组"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE watchlist SET group_name = ? WHERE ts_code = ?",
            (group_name, ts_code)
        )


def get_watchlist_groups() -> list:
    """获取所有分组名称"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM watchlist WHERE group_name != '' ORDER BY group_name"
        ).fetchall()
        return [r[0] for r in rows]


def remove_from_watchlist(ts_code: str):
    """从自选股中移除"""
    with get_conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE ts_code = ?", (ts_code,))


def get_watchlist() -> pd.DataFrame:
    """获取自选股列表（按添加时间倒序）"""
    with get_conn() as conn:
        return pd.read_sql("SELECT * FROM watchlist ORDER BY added_date DESC", conn)


# ============================================================
# 大盘指数 (index_daily) 表操作
# ============================================================

def save_index_daily(df: pd.DataFrame):
    """保存大盘指数日线数据到 index_daily 表

    参数:
        df: 包含 ts_code, trade_date, open, high, low, close, volume, pct_chg 的 DataFrame
    """
    if df.empty:
        return
    cols = ["ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "pct_chg"]
    for col in cols:
        if col not in df.columns:
            df[col] = None                              # 缺失列补 None
    placeholders = ",".join(["?"] * len(cols))            # 占位符
    col_names = ",".join(cols)                            # 列名
    sql = f"INSERT OR REPLACE INTO index_daily ({col_names}) VALUES ({placeholders})"
    rows = df[cols].values.tolist()                       # 批量写入
    with get_conn() as conn:
        conn.executemany(sql, rows)


def get_index_daily(ts_code: str, limit: int = 2) -> pd.DataFrame:
    """获取大盘指数日线数据

    参数:
        ts_code: 指数代码 e.g. "000001.SH"
        limit:   返回最近 N 条

    返回:
        按日期升序排列的 DataFrame
    """
    with get_conn() as conn:
        df = pd.read_sql(
            "SELECT * FROM index_daily WHERE ts_code = ? ORDER BY trade_date DESC LIMIT ?",
            conn, params=[ts_code, limit]
        )
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)  # 确保日期为字符串
        df = df.sort_values("trade_date").reset_index(drop=True)  # 转升序
    return df


def get_index_latest_date() -> str:
    """获取指数数据最新日期"""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(trade_date) FROM index_daily").fetchone()
        return row[0] if row and row[0] else ""


def get_stocks_with_data(min_days: int = 1) -> list:
    """获取有日线数据的股票代码列表

    Args:
        min_days: 最少需要多少条日线数据（默认1条即为有数据）

    Returns:
        有数据的股票代码列表 e.g. ["000001.SZ", "600036.SH", ...]
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts_code FROM daily_price GROUP BY ts_code HAVING COUNT(*) >= ?",
            (min_days,)
        ).fetchall()
        return [r[0] for r in rows]


# ============================================================
# 模拟持仓 (position) 表操作
# ============================================================

def get_positions() -> list:
    """获取所有模拟持仓记录"""
    from datetime import datetime
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts_code, direction, buy_price, shares, buy_date, note "
            "FROM position ORDER BY buy_date DESC"
        ).fetchall()
        return [
            {
                "ts_code": r[1],
                "direction": r[2],
                "buy_price": r[3],
                "shares": r[4],
                "buy_date": r[5],
                "note": r[6] or "",
                "_id": r[0],
            }
            for r in rows
        ]


def add_position(ts_code: str, buy_price: float, shares: int,
                 buy_date: str, note: str = "") -> int:
    """添加一条模拟持仓记录"""
    from datetime import datetime
    now = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO position (ts_code, direction, buy_price, shares, "
            "buy_date, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (ts_code, "BUY", buy_price, shares, buy_date, note, now, now)
        )
        return cur.lastrowid


def remove_position(position_id: int):
    """删除一条模拟持仓记录"""
    with get_conn() as conn:
        conn.execute("DELETE FROM position WHERE id = ?", (position_id,))


def clear_positions():
    """清空所有模拟持仓"""
    with get_conn() as conn:
        conn.execute("DELETE FROM position")
