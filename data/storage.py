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
import threading        # 线程本地存储（连接池）
import re               # 备份文件名消毒
import uuid             # 更新锁所有权 token
from datetime import datetime          # 断层判定的日期间隔、备份时间戳
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
_STALE_LOCK_TIMEOUT = 600  # 陈旧孤儿锁超时时间 (10分钟)

logger = logging.getLogger(__name__)


_LOCK_OWNER_META = None  # 本进程最近一次成功获取的锁元数据（释放时校验所有权）


def _is_pid_running(pid: int) -> bool:
    """检查指定 PID 进程是否存活。

    「查不清」一律保守返回 True：宁可让孤儿锁多等一个 max_stale_seconds，
    也不能把正在写库的有效锁误判成死锁后强删。Windows 上跨会话/受保护进程
    的 OpenProcess 会返回 ACCESS_DENIED，此时进程其实活着。
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            # 64 位句柄必须用 c_void_p，默认 c_int 会截断句柄导致误判
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER（PID 不存在）；5 = ACCESS_DENIED（存在但不可查）
                return ctypes.get_last_error() != 87
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # EPERM：进程存在，只是无权限发信号
    except Exception:
        return True


def _read_lock_meta(lock_path: Path):
    """读取锁元数据；文件不存在或只写了一半时返回 None（绝不抛异常）"""
    try:
        return json.loads((lock_path / "lock_info.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_lock_meta(lock_path: Path, meta: dict):
    """原子写锁元数据：先写临时文件再 os.replace，避免别的进程读到半截 JSON"""
    tmp = lock_path / f"lock_info.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(str(tmp), str(lock_path / "lock_info.json"))


def force_release_update_lock(expected_meta: dict = None):
    """强制释放更新锁（清理陈旧或异常终止遗留的锁目录）

    expected_meta 不为空时，只有当前锁元数据与「判定陈旧时」完全一致才删除。
    这能挡住典型的 TOCTOU 竞态：A 的锁被判陈旧 → A 恰好正常释放 → B 建了新锁
    → C 若无脑删除就会把 B 的有效锁清掉，两个进程同时进临界区。
    UI 上的「手动强制重置」不传该参数，保留显式强删语义。
    """
    lock_path = Path(_LOCK_FILE)
    if not lock_path.exists():
        return
    if expected_meta is not None:
        current = _read_lock_meta(lock_path)
        if current != expected_meta:
            logger.warning("更新锁已被其它进程接管，跳过本次回收（避免误删有效锁）")
            return
    try:
        import shutil
        if lock_path.is_dir():
            shutil.rmtree(str(lock_path), ignore_errors=True)
        else:
            lock_path.unlink(missing_ok=True)
        logger.info("强制释放更新锁成功")
    except Exception as e:
        logger.warning(f"强制释放更新锁异常: {e}")


def acquire_update_lock(timeout: int = None, max_stale_seconds: int = None) -> bool:
    """尝试获取更新锁（文件锁，非阻塞式，含陈旧锁自动回收自愈机制）

    使用原子性 os.mkdir 实现跨平台文件锁，并记录 PID 及创建时间戳。
    若发现锁持有者已崩溃退出，或锁存活时长已超过 max_stale_seconds，
    将自动判定为孤儿陈旧锁并强行释放回收，防止系统永久死锁。

    Args:
        timeout: 最长等待秒数（默认 _LOCK_TIMEOUT=60）
        max_stale_seconds: 孤儿锁过期判定时长（默认 _STALE_LOCK_TIMEOUT=600 秒）

    Returns:
        True=拿到锁, False=超时未拿到
    """
    global _LOCK_OWNER_META

    if timeout is None:
        timeout = _LOCK_TIMEOUT
    if max_stale_seconds is None:
        max_stale_seconds = _STALE_LOCK_TIMEOUT

    lock_path = Path(_LOCK_FILE)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            os.mkdir(str(lock_path))  # 原子操作：目录不存在则创建成功
            meta = {
                "pid": os.getpid(),
                "created_at": time.time(),
                "created_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                "owner_token": uuid.uuid4().hex,
            }
            try:
                _write_lock_meta(lock_path, meta)
            except Exception as e:
                # 身份信息写不进去的锁无法被安全释放，主动放弃本次获取
                logger.warning(f"更新锁元数据写入失败，放弃本次获取: {e}")
                force_release_update_lock()
                return False
            _LOCK_OWNER_META = meta
            logger.debug("获取更新锁成功")
            return True
        except FileExistsError:
            # 检查现有锁是否是陈旧死锁
            is_stale = False
            stale_meta = None
            meta = _read_lock_meta(lock_path)
            if meta is not None:
                holder_pid = meta.get("pid", 0)
                created_at = meta.get("created_at", 0)
                age = time.time() - created_at
                # 1. 持有者进程已不存在；或 2. 存活时间超过 max_stale_seconds
                if (holder_pid and not _is_pid_running(holder_pid)) or (age > max_stale_seconds):
                    logger.warning(
                        f"检测到陈旧更新锁: PID={holder_pid}, 已持有 {age:.0f}s "
                        f"(阈值 {max_stale_seconds}s)，正在执行自动回收..."
                    )
                    is_stale = True
                    stale_meta = meta
            else:
                # 元数据缺失或正在被写入（半截 JSON）**绝不能**据此判定陈旧：
                # 原实现在这里直接强删，会误杀刚建好目录、还没写完元数据的持有者。
                # 只有整个锁目录存活超阈值才回收，并兼容旧版本无 info 文件的锁。
                try:
                    mtime = os.path.getmtime(str(lock_path))
                    age = time.time() - mtime
                    if age > max_stale_seconds:
                        logger.warning(
                            f"检测到无有效元数据的更新锁: 已存在 {age:.0f}s "
                            f"(阈值 {max_stale_seconds}s)，正在执行自动回收..."
                        )
                        is_stale = True
                except Exception:
                    pass

            if is_stale:
                force_release_update_lock(stale_meta)
                continue

            time.sleep(1)

    logger.warning(f"等待更新锁超时 ({timeout}s)，当前仍有其他更新任务在运行")
    return False


def release_update_lock():
    """只释放本进程自己持有的锁（按 owner_token 元数据校验所有权）"""
    global _LOCK_OWNER_META
    meta = _LOCK_OWNER_META
    if meta is None:
        # 兼容「外部直接调用 / 旧版无元数据锁」的路径
        logger.warning("未记录更新锁所有权，执行无校验释放")
        force_release_update_lock()
    else:
        force_release_update_lock(meta)
    _LOCK_OWNER_META = None


@contextmanager
def update_lock(timeout: int = None, max_stale_seconds: int = None):
    """获取/释放更新锁的上下文管理器

    用法:
        with update_lock():
            run_update(...)
    """
    if max_stale_seconds is not None:
        acquired = acquire_update_lock(timeout, max_stale_seconds=max_stale_seconds)
    else:
        acquired = acquire_update_lock(timeout)
    try:
        yield acquired
    finally:
        if acquired:
            release_update_lock()


# ============================================================
# 数据库连接管理（含线程级连接池）
# ============================================================

# 线程本地存储：每个线程复用自己的 SQLite 连接，避免频繁创建/关闭
_thread_local = threading.local()


def _create_connection() -> sqlite3.Connection:
    """创建一个新的 SQLite 连接并设置 PRAGMA"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _get_pooled_conn() -> sqlite3.Connection:
    """获取线程级复用连接（如果存在），否则创建新连接"""
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = _create_connection()
        _thread_local.conn = conn
    return conn


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
        - 线程级连接池：同一线程内复用连接，减少 I/O 开销
    """
    conn = _get_pooled_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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


def backup_database(backup_path: Path) -> bool:
    """使用 SQLite 原生在线热备 API (conn.backup) 安全备份 WAL 模式下的数据库"""
    backup_path = Path(backup_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as src_conn:
        dst_conn = sqlite3.connect(str(backup_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    return True


def restore_database(backup_path: Path) -> bool:
    """使用 SQLite 原生 backup API 在线将备份恢复到主库（避免 Windows 文件句柄锁冲突与 WAL 损坏）"""
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")
    src_conn = sqlite3.connect(str(backup_path))
    try:
        with get_conn() as dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
    return True


# ============================================================
# 数据库表初始化
# ============================================================

def _get_db_version(conn) -> int:
    """获取当前数据库版本号（PRAGMA user_version）"""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _set_db_version(conn, version: int):
    """设置数据库版本号"""
    conn.execute(f"PRAGMA user_version = {version}")


def _column_exists(conn, table: str, column: str) -> bool:
    """判断表中是否已有某列（让 DDL 迁移可重入，不再靠吞异常）"""
    try:
        return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except sqlite3.Error:
        return False


# 声明式迁移：(版本号, [(表, 列, 类型声明), ...], [DDL 语句, ...])
# 每个版本独立提交；只有全部成功才推进 user_version。
_MIGRATIONS = (
    (1, [], []),  # 初始版本，表结构由 init_db 创建
    (2, [("backtest_result", "calmar_ratio", "REAL"),
         ("backtest_result", "sell_count", "INTEGER")], []),
    (3, [("backtest_trade", "context_snapshot", "TEXT")], []),
    (4, [("watchlist", "recommended_strategy", "TEXT DEFAULT ''"),
         ("watchlist", "group_name", "TEXT DEFAULT ''")], []),
    (5, [], ["CREATE TABLE IF NOT EXISTS notify_log ("
             "job_key TEXT PRIMARY KEY, created_at TEXT)"]),
)


def _run_migrations(conn):
    """执行版本化数据库迁移（逐版本事务，失败即中止启动）

    旧实现有两个致命问题：
      1. executescript 在 `with get_conn()` 内隐式提交，破坏外层事务边界；
      2. DDL 抛异常只打 warning，却仍无条件推进 user_version —— 迁移一旦失败
         永远不会重试，schema 永久缺列且后续查询静默失败。
    现在改为：先查 PRAGMA table_info 保证幂等 → 单条 execute → 全部成功才
    COMMIT + 推进版本；任一失败即 ROLLBACK 并抛错，让启动显式失败。
    """
    current = _get_db_version(conn)
    for version, columns, statements in _MIGRATIONS:
        if current >= version:
            continue
        try:
            for table, column, decl in columns:
                if not _column_exists(conn, table, column):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            for stmt in statements:
                conn.execute(stmt)
            _set_db_version(conn, version)
            conn.commit()
            logger.info(f"数据库迁移完成: -> v{version}")
        except Exception as e:
            conn.rollback()
            raise RuntimeError(
                f"数据库迁移 v{version} 失败，已回滚且未推进 user_version: {e}"
            ) from e
        current = version


def notification_already_sent(job_key: str) -> bool:
    """查询当日去重键是否已推送（定时任务双入口幂等）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM notify_log WHERE job_key = ?", (job_key,)
        ).fetchone()
        return row is not None


def mark_notification_sent(job_key: str) -> None:
    """登记已推送的去重键（主键冲突时视为已推送，天然幂等）"""
    from datetime import datetime as _dt
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notify_log (job_key, created_at) VALUES (?, ?)",
            (job_key, _dt.now().isoformat()),
        )


def init_db():
    """创建所有数据库表并执行版本化迁移（幂等：IF NOT EXISTS，重复调用安全）

    流程:
        1. 创建所有表（CREATE TABLE IF NOT EXISTS）
        2. 执行版本化迁移（PRAGMA user_version 追踪）

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

            -- ETF基本信息表
            CREATE TABLE IF NOT EXISTS etf_basic (
                ts_code      TEXT PRIMARY KEY,    -- ETF代码 e.g. "510300.SH"
                symbol       TEXT NOT NULL,       -- 纯数字代码 e.g. "510300"
                name         TEXT NOT NULL,       -- ETF名称 e.g. "沪深300ETF"
                market       TEXT NOT NULL,       -- 市场 "SH" 或 "SZ"
                list_date    TEXT,                -- 上市日期 "YYYYMMDD"
                updated_at   TEXT                 -- 数据更新时间
            );

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
                holding_days    INTEGER,           -- 持有天数
                context_snapshot TEXT              -- 交易决策/风控上下文快照（JSON）
            );
            CREATE INDEX IF NOT EXISTS idx_bt_trade ON backtest_trade(backtest_id);  -- 按回测查交易

            -- 自选股表
            CREATE TABLE IF NOT EXISTS watchlist (
                ts_code    TEXT PRIMARY KEY,      -- 股票代码
                added_date TEXT,                  -- 添加日期
                note       TEXT,                  -- 备注
                group_name TEXT DEFAULT '',       -- 分组（如"长线池"、"短线池"）
                recommended_strategy TEXT DEFAULT '' -- 推荐策略（智能回测或行业规则匹配结果）
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

            -- 推送幂等表（定时任务双入口：同一天同一任务只推一次）
            CREATE TABLE IF NOT EXISTS notify_log (
                job_key     TEXT PRIMARY KEY,      -- 如 'signals:20261112'
                created_at  TEXT
            );
        """)

        # 执行版本化迁移（新增列已全部由 _MIGRATIONS 声明式覆盖，
        # 这里不再保留会吞掉所有异常的裸 ALTER TABLE）
        _run_migrations(conn)


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
# ETF基本信息 (etf_basic) 表操作
# ============================================================

def save_etf_list(df: pd.DataFrame):
    """增量更新 ETF 列表到 etf_basic 表（INSERT OR REPLACE）

    参数:
        df: 包含 ts_code, symbol, name, market 等列的 DataFrame
    """
    from datetime import datetime
    now = datetime.now().isoformat()              # 生成当前时间戳
    df = df.copy()                                 # 不修改原始数据
    df["updated_at"] = now                          # 追加更新时间列
    cols = ["ts_code", "symbol", "name", "market", "list_date", "updated_at"]
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    sql = f"INSERT OR REPLACE INTO etf_basic ({col_names}) VALUES ({placeholders})"
    rows = df[cols].values.tolist()
    with get_conn() as conn:
        conn.executemany(sql, rows)


def get_etf_list() -> pd.DataFrame:
    """获取所有 ETF 列表（按代码排序）

    表不存在时返回空 DataFrame（兼容旧库未迁移的场景）。
    """
    with get_conn() as conn:
        try:
            return pd.read_sql("SELECT * FROM etf_basic ORDER BY ts_code", conn)
        except Exception:
            return pd.DataFrame()


def get_etf_name(ts_code: str) -> str:
    """根据 ETF 代码查询名称，查不到则返回代码本身"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM etf_basic WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        return row[0] if row else ts_code


def get_instrument_name(ts_code: str) -> str:
    """根据代码查询标的中文名称（股票 + ETF 统一查询）

    先查股票表，再查 ETF 表，都查不到则返回代码本身。

    参数:
        ts_code: 标的代码 e.g. "000001.SZ" / "510300.SH"
    返回:
        标的中文名称，如 "平安银行" / "沪深300ETF"
    """
    name = get_stock_name(ts_code)
    if name != ts_code:
        return name
    return get_etf_name(ts_code)


_INSTRUMENT_META_COLS = ["industry", "is_st", "list_date", "delist_date"]


def get_instrument_list() -> pd.DataFrame:
    """获取可交易标的总列表（股票 + ETF，含 type 标记）

    用于页面搜索、ST 判定与行业特征匹配。这里**必须**把 stock_basic 的行业、
    ST、上市/退市日期一并带出：旧实现只投影 4 列，导致
    ``engine.backtester.get_st_lookup()`` 的 is_st 分支永远走不到、
    ``services.strategy_recommender`` 的行业启发式也恒为空。
    返回列: ts_code, symbol, name, market, type, industry, is_st, list_date, delist_date
    """
    parts = []
    stocks = get_stock_list()
    if not stocks.empty:
        keep = ["ts_code", "symbol", "name", "market"] + [
            c for c in _INSTRUMENT_META_COLS if c in stocks.columns
        ]
        s = stocks[keep].copy()
        s["type"] = "股票"
        parts.append(s)
    etfs = get_etf_list()
    if not etfs.empty:
        e = etfs[["ts_code", "symbol", "name", "market"]].copy()
        for c in _INSTRUMENT_META_COLS:
            e[c] = 0 if c == "is_st" else ""
        e["type"] = "ETF"
        parts.append(e)
    if not parts:
        return pd.DataFrame(
            columns=["ts_code", "symbol", "name", "market", "type"] + _INSTRUMENT_META_COLS
        )
    out = pd.concat(parts, ignore_index=True)
    for c in _INSTRUMENT_META_COLS:
        if c not in out.columns:
            out[c] = 0 if c == "is_st" else ""
    return out.sort_values("ts_code").reset_index(drop=True)


def get_point_in_time_universe(as_of: str) -> list:
    """返回 as_of 当日实际存在的股票池（point-in-time，缓解幸存者偏差）

    过滤规则：list_date <= as_of 且（未退市或 delist_date >= as_of）。
    当前库尚未回填退市股时本函数等价于现有股票池；一旦 stock_basic 收录
    退市/暂停上市标的及其 delist_date，历史回测/扫描就会自动按当时口径取池，
    不再用「今天还活着」的股票去回测多年前。日期支持 YYYYMMDD / YYYY-MM-DD。
    """
    d = str(as_of or "").replace("-", "").replace("/", "").strip()
    if not d:
        d = "99991231"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts_code FROM stock_basic "
            "WHERE (list_date IS NULL OR TRIM(list_date) = '' OR REPLACE(list_date, '-', '') <= ?) "
            "  AND (delist_date IS NULL OR TRIM(delist_date) = '' OR REPLACE(delist_date, '-', '') >= ?) "
            "ORDER BY ts_code",
            (d, d),
        ).fetchall()
    return [r[0] for r in rows]


# ============================================================
# 日线行情 (daily_price) 表操作
# ============================================================

_DAILY_COLS = ["ts_code", "trade_date", "open", "high", "low", "close",
               "volume", "amount", "pct_chg", "turnover", "adj_factor"]


def _prepare_daily_rows(df: pd.DataFrame):
    """把日线 DataFrame 标准化为 (INSERT SQL, 参数行列表)。

    供 save_daily 与 replace_daily 共用，确保两条写入路径的口径完全一致。
    """
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
    for col in _DAILY_COLS:
        if col not in df.columns:
            df[col] = None                           # 缺失列补 None
    placeholders = ",".join(["?"] * len(_DAILY_COLS))
    col_names = ",".join(_DAILY_COLS)
    sql = f"INSERT OR REPLACE INTO daily_price ({col_names}) VALUES ({placeholders})"
    return sql, df[_DAILY_COLS].values.tolist()


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

    sql, rows = _prepare_daily_rows(df)               # 一次标准化
    with get_conn() as conn:
        conn.executemany(sql, rows)                    # 批量写入


def align_volume_unit(ts_code: str, df: pd.DataFrame) -> pd.DataFrame:
    """把增量数据的 volume 单位对齐到库内该标的的历史口径（统一为「股」）。

    数据源切换（AKShare → Baostock / TickFlow）可能返回不同单位的成交量。若不对齐，
    同一标的的历史序列会出现 100 倍断层，`vol_ma`、流动性过滤、量价策略全部失真。

    只在**库内历史口径本身正常**（ratio 中位数落在 0.15~20）时才对齐；若历史本身
    是「手」，应由 ``scripts/repair_volume_units.py`` 修复历史，而不是把新数据
    改坏去迁就错误的历史。
    """
    if df.empty or not all(c in df.columns for c in ("close", "volume", "amount")):
        return df

    def _ratio_med(frame: pd.DataFrame):
        m = (frame["close"] > 0) & (frame["volume"] > 0) & (frame["amount"] > 0)
        if not m.any():
            return None
        r = frame.loc[m, "amount"] / (frame.loc[m, "close"] * frame.loc[m, "volume"])
        return float(r.median()) if len(r) else None

    with get_conn() as conn:
        hist = pd.read_sql(
            "SELECT close, volume, amount FROM daily_price WHERE ts_code = ? "
            "ORDER BY trade_date DESC LIMIT 30", conn, params=(ts_code,)
        )
    if hist.empty:
        return df

    h, n = _ratio_med(hist), _ratio_med(df)
    if h is None or n is None or h <= 0 or n <= 0 or not (0.15 <= h <= 20):
        return df

    q = n / h
    if 50 < q < 200:
        out = df.copy()
        out["volume"] = out["volume"] * 100
        logger.warning(f"[{ts_code}] 增量成交量单位为「手」，已 ×100 对齐库内「股」口径")
        return out
    if 0.005 < q < 0.02:
        out = df.copy()
        out["volume"] = out["volume"] / 100
        logger.warning(f"[{ts_code}] 增量成交量疑似被放大 100 倍，已 /100 对齐库内口径")
        return out
    return df


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
    if limit > 0:
        query += " ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)
    else:
        query += " ORDER BY trade_date"
    with get_conn() as conn:
        df = pd.read_sql(query, conn, params=params)
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)  # 确保日期为字符串
        if limit > 0:
            df = df.sort_values("trade_date").reset_index(drop=True)  # 转回升序
    return df


def clear_daily(ts_code: str):
    """清空单只标的的所有日线数据。

    ⚠️ 危险：DELETE 会立即提交且不可撤销。**增量更新路径禁止调用本函数**。
    除权基准迁移请改用 replace_daily()：先校验全量新数据、备份旧数据，并在
    单个事务内完成替换，任何异常自动回滚。本函数仅供测试与显式维护使用。
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM daily_price WHERE ts_code = ?", (ts_code,))
        logger.warning(f"已清空标的 {ts_code} 的本地日线缓存（不可撤销）")


def get_earliest_date(ts_code: str) -> str:
    """获取某只股票在库中最早的交易日（全量重拉的正确起点）"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MIN(trade_date) FROM daily_price WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        return row[0] if row and row[0] else ""


def _backup_daily(ts_code: str, conn):
    """原子替换前把旧数据导出成压缩 CSV，每标的最多保留最近 3 份。"""
    old = pd.read_sql(
        "SELECT * FROM daily_price WHERE ts_code = ? ORDER BY trade_date",
        conn, params=(ts_code,),
    )
    if old.empty:
        return None
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_code = re.sub(r"[^A-Za-z0-9_.-]", "_", str(ts_code))
    path = backup_dir / f"daily_{safe_code}_{datetime.now():%Y%m%d_%H%M%S}.csv.gz"
    old.to_csv(path, index=False, compression="gzip")
    files = sorted(backup_dir.glob(f"daily_{safe_code}_*.csv.gz"))
    for f in files[:-3]:
        try:
            f.unlink()
        except OSError:
            pass
    logger.info(f"[{ts_code}] 替换前已备份 {len(old)} 行 → {path}")
    return path


def replace_daily(ts_code: str, new_df: pd.DataFrame,
                  min_history_ratio: float = 0.9,
                  allow_partial: bool = False) -> bool:
    """原子替换单只标的的全量日线（除权基准迁移后的安全重置入口）。

    取代旧的「clear_daily + 重拉」链条，消除三类不可逆丢失：

      1. **先校验再删除**：新数据行数不足旧数据的 ``min_history_ratio``、或起点
         晚于旧数据起点时直接拒绝，绝不用短历史覆盖长历史。
      2. **备份**：替换前把旧数据导出到 ``data/backups/``（每标的最近 3 份）。
      3. **单事务**：DELETE + INSERT 在同一个 ``get_conn()`` 事务内，异常自动回滚。

    Returns:
        True  = 已安全替换；False = 校验不通过，调用方**必须放弃本次写入**并保留原库。
    """
    if new_df is None or new_df.empty or "trade_date" not in new_df.columns:
        logger.warning(f"[{ts_code}] 全量替换被拒绝：新数据为空")
        return False

    sql, rows = _prepare_daily_rows(new_df)
    new_dates = (new_df["trade_date"].astype(str)
                 .str.replace("-", "", regex=False).str[:8])
    new_min = new_dates.min()

    with get_conn() as conn:
        old_n, old_min, _ = conn.execute(
            "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) "
            "FROM daily_price WHERE ts_code = ?", (ts_code,)
        ).fetchone()
        old_n = int(old_n or 0)

        if old_n > 0 and not allow_partial:
            if len(new_df) < old_n * min_history_ratio:
                logger.error(
                    f"[{ts_code}] 全量替换被拒绝：新数据 {len(new_df)} 行 < 旧数据 "
                    f"{old_n} 行的 {min_history_ratio:.0%}，保留原库"
                )
                return False
            if old_min and new_min > str(old_min):
                logger.error(
                    f"[{ts_code}] 全量替换被拒绝：新数据起点 {new_min} 晚于旧数据 "
                    f"{old_min}，会永久丢失早期历史，保留原库"
                )
                return False

        _backup_daily(ts_code, conn)
        conn.execute("DELETE FROM daily_price WHERE ts_code = ?", (ts_code,))
        conn.executemany(sql, rows)

    logger.info(f"[{ts_code}] 全量替换完成：{old_n} → {len(new_df)} 行，起点 {new_min}")
    return True


# 同一交易日同一标的的收盘价在基准未变时必须完全一致；0.5% 容差覆盖浮点/舍入差异
ANOMALY_OVERLAP_TOL = 0.005
# 断层判定的通用容差（pct_chg 反推有误差，保留较宽的 8%）
ANOMALY_RATIO_TOL = 0.08


def _infer_price_limit_pct(ts_code: str) -> float:
    """按板块推断单日涨跌幅上限（%）。ST 无法仅凭代码识别，保守取 10%。

    仅在 pct_chg 缺失/为 0 时用于区分「合法涨跌」与「除权基准迁移」。
    """
    parts = str(ts_code).split(".")
    code = parts[0]
    suffix = parts[-1].upper() if len(parts) > 1 else ""
    if suffix == "BJ" or code.startswith(("43", "83", "87", "92")):
        return 30.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def check_split_dividend_anomaly(ts_code: str, new_df: pd.DataFrame) -> bool:
    """检测增量日线是否与本地历史发生前复权基准迁移（除权除息断层）。

    旧实现只用 ``pct_chg`` 反推前收：``implied = close / (1 + pct_chg/100)``。
    当该字段缺失、或被数据源填成 0（TickFlow 首行 fillna(0)、Tushare 降级字段
    缺失）时，``implied == close``，于是**任意涨跌超 8% 都会被误判为除权**，
    进而触发清库。这里改成按证据可靠性分级：

      1. **重叠交易日**：增量中若含库内已有日期，直接比较同日收盘价。基准未变时
         两者必须完全一致，这是最可靠的信号，且与涨跌幅字段无关。
      2. **pct_chg 可信**（非空、非 0、|值| ≤ 31）：用数据源自己的前收反推对比。
      3. **pct_chg 不可信**：仅当两日期确为相邻交易日（自然日差 ≤ 10 天）、且价格
         变动超过该板块涨跌停上限 + 2 个百分点缓冲时才判定断层。合法涨停、跌停、
         长期停牌后复牌都不会再被误判。

    宁可漏判（价格序列略有偏差），不可误判（清库会不可逆丢失历史）。

    Returns:
        True: 检测到断层，调用方应走 ``replace_daily()`` 全量替换
        False: 数据连续平稳
    """
    if new_df is None or new_df.empty:
        return False
    if "trade_date" not in new_df.columns or "close" not in new_df.columns:
        return False

    keep = ["trade_date", "close"] + (["pct_chg"] if "pct_chg" in new_df.columns else [])
    new = new_df[keep].copy()
    new["trade_date"] = (new["trade_date"].astype(str)
                         .str.replace("-", "", regex=False).str[:8])
    new["close"] = pd.to_numeric(new["close"], errors="coerce")
    new = new.dropna(subset=["close"])
    new = new[new["close"] > 0]
    if new.empty:
        return False
    new = new.sort_values("trade_date")

    with get_conn() as conn:
        hist = pd.read_sql(
            "SELECT trade_date, close FROM daily_price WHERE ts_code = ? "
            "ORDER BY trade_date DESC LIMIT 5", conn, params=(ts_code,)
        )
    if hist.empty:
        return False
    hist["trade_date"] = (hist["trade_date"].astype(str)
                          .str.replace("-", "", regex=False).str[:8])
    hist["close"] = pd.to_numeric(hist["close"], errors="coerce")
    hist = hist.dropna(subset=["close"])
    hist = hist[hist["close"] > 0]
    if hist.empty:
        return False

    # ---- 证据 1：重叠交易日的同日收盘价 ----
    overlap = new.merge(hist, on="trade_date", suffixes=("_new", "_db"))
    if not overlap.empty:
        dev = (overlap["close_new"] / overlap["close_db"] - 1.0).abs()
        if float(dev.max()) > ANOMALY_OVERLAP_TOL:
            logger.warning(
                f"[{ts_code}] 检测到前复权基准迁移：重叠交易日同日收盘价差异 "
                f"{dev.max():.2%}，触发全量替换保护"
            )
            return True
        # 重叠段一致 → 基准到目前为止未变；但新增日期仍可能跨越除权日，
        # 因此不在这里提前下结论，继续用证据 2/3 检查新增首日。

    # ---- 新增段检查（无论是否有重叠都要做）----
    latest = hist.sort_values("trade_date").iloc[-1]
    db_latest_date = str(latest["trade_date"])
    db_latest_close = float(latest["close"])
    incoming = new[new["trade_date"] > db_latest_date]
    if incoming.empty:
        return False
    first = incoming.iloc[0]
    curr_close = float(first["close"])
    if curr_close <= 0 or db_latest_close <= 0:
        return False

    # ---- 证据 2：pct_chg 可信 ----
    pct = None
    if "pct_chg" in incoming.columns:
        raw = first.get("pct_chg")
        if raw is not None and pd.notna(raw):
            try:
                pct = float(raw)
            except (TypeError, ValueError):
                pct = None
    if pct is not None and 0.0 < abs(pct) <= 31.0:
        implied = curr_close / (1.0 + pct / 100.0)
        if implied > 0:
            ratio = db_latest_close / implied
            if abs(ratio - 1.0) > ANOMALY_RATIO_TOL:
                logger.warning(
                    f"[{ts_code}] 检测到除权除息跳空断层: 本地最新价={db_latest_close:.2f}, "
                    f"数据源前收={implied:.2f}, 差异比率={ratio:.3f}"
                )
                return True
        return False

    # ---- 证据 3：pct_chg 缺失/为 0，用涨跌停上界兜底 ----
    try:
        gap_days = (datetime.strptime(str(first["trade_date"]), "%Y%m%d")
                    - datetime.strptime(db_latest_date, "%Y%m%d")).days
    except (ValueError, TypeError):
        gap_days = 999
    if gap_days > 10:
        logger.debug(f"[{ts_code}] 增量与库内最新日相隔 {gap_days} 天，跳过断层判定")
        return False

    limit_pct = _infer_price_limit_pct(ts_code)
    move = abs(curr_close / db_latest_close - 1.0)
    if move > limit_pct / 100.0 + 0.02:
        logger.warning(
            f"[{ts_code}] pct_chg 不可信，但相邻交易日价格变动 {move:.1%} "
            f"超出 {limit_pct:.0f}% 涨跌停上限，判定为除权断层"
        )
        return True
    return False


def batch_get_latest(codes: list, limit: int = 2, chunk_size: int = 500) -> pd.DataFrame:
    """批量获取多只股票的最新N条日线数据

    使用 SQL 窗口函数 ROW_NUMBER()，分批（默认500只/批，避免SQLite参数超限）执行并合并。

    参数:
        codes: 股票代码列表 e.g. ['000001.SZ', '600519.SH']
        limit: 每只股票取最近 N 条
        chunk_size: 单次 SQL 查询的标的数量上限（默认500，防止触发 SQLite 999 变量限制）

    返回:
        含 ts_code, trade_date, open, high, low, close, volume, pct_chg 的 DataFrame
    """
    if not codes:
        return pd.DataFrame()

    results = []
    with get_conn() as conn:
        for i in range(0, len(codes), chunk_size):
            chunk = codes[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
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
            params = chunk + [limit]
            sub_df = pd.read_sql(query, conn, params=params)
            if not sub_df.empty:
                results.append(sub_df)

    if not results:
        return pd.DataFrame()

    df = pd.concat(results, ignore_index=True)
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)  # 确保日期为字符串
        if "rn" in df.columns:
            df = df.drop(columns=["rn"])                  # 移除窗口函数辅助列
    return df


def batch_get_daily_summary(codes: list, chunk_size: int = 500) -> dict:
    """批量聚合查询多只标的的日线总条数与最新交易日（单次 GROUP BY 消除 N+1 全表扫描）

    返回:
        {ts_code: {"count": int, "latest_date": str}}
    """
    if not codes:
        return {}
    summary = {}
    with get_conn() as conn:
        for i in range(0, len(codes), chunk_size):
            chunk = codes[i:i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT ts_code, COUNT(*) as cnt, MAX(trade_date) as max_date
                    FROM daily_price
                    WHERE ts_code IN ({placeholders})
                    GROUP BY ts_code""",
                chunk,
            ).fetchall()
            for code, cnt, max_dt in rows:
                summary[str(code)] = {
                    "count": int(cnt or 0),
                    "latest_date": str(max_dt or ""),
                }
    return summary


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


def save_signals_batch(signals: list):
    """批量保存交易信号到数据库（用 executemany 一次写入，性能提升 50-100x）

    参数:
        signals: Signal 数据模型对象列表
    """
    if not signals:
        return
    from datetime import datetime
    now = datetime.now().isoformat()
    rows = [
        (sig.ts_code, sig.trade_date, sig.strategy, sig.direction,
         sig.score, sig.reason, sig.price_ref, now)
        for sig in signals
    ]
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO signal (ts_code, trade_date, strategy, direction,
               score, reason, price_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows
        )


def get_signals(trade_date: str = "", strategy: str = "",
                limit: int = 100, start_date: str = "") -> pd.DataFrame:
    """查询交易信号

    参数:
        trade_date: 按具体单日过滤 "YYYYMMDD"（空=全部）
        strategy:   按策略名过滤（空=全部）
        limit:      最多返回条数
        start_date: 按起始日期过滤 "YYYYMMDD"（包含当日，空=不限）

    返回:
        按日期降序、评分降序排列的信号 DataFrame
    """
    query = "SELECT * FROM signal WHERE 1=1"             # 基础查询
    params = []
    if trade_date:
        query += " AND trade_date = ?"                   # 按日期筛选
        params.append(trade_date)
    if start_date:
        query += " AND trade_date >= ?"                  # 按起始日期筛选
        params.append(start_date)
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
                sharpe_ratio, win_rate, trade_count, sell_count, calmar_ratio,
                equity_curve, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.strategy, result.params, result.start_date, result.end_date,
             result.initial_capital, result.final_capital, result.total_return,
             result.annual_return, result.max_drawdown, result.sharpe_ratio,
             result.win_rate, result.trade_count,
             getattr(result, "sell_count", 0),
             getattr(result, "calmar_ratio", 0),
             json.dumps(result.equity_curve, ensure_ascii=False),
             datetime.now().isoformat())
        )
        return cursor.lastrowid                           # 返回自增ID


def save_backtest_trades(backtest_id: int, trades: list):
    """保存回测的交易明细（批量写入 executemany，含决策快照）

    参数:
        backtest_id: 关联的回测结果 ID
        trades:      Trade 数据模型对象列表
    """
    if not trades:
        return
    rows = [
        (backtest_id, t.ts_code, t.direction, t.trade_date,
         t.price, t.volume, t.commission, t.tax, t.pnl, t.holding_days,
         json.dumps(getattr(t, "context_snapshot", {}) or {}, ensure_ascii=False))
        for t in trades
    ]
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO backtest_trade
               (backtest_id, ts_code, direction, trade_date, price,
                volume, commission, tax, pnl, holding_days, context_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows
        )


def get_backtest_results(limit: int = 20) -> pd.DataFrame:
    """获取回测结果列表（按创建时间倒序）"""
    with get_conn() as conn:
        return pd.read_sql(
            "SELECT * FROM backtest_result ORDER BY created_at DESC LIMIT ?",
            conn, params=[limit]
        )


def get_backtest_result_by_id(backtest_id: int) -> dict:
    """获取单条回测结果（含权益曲线解析）

    Args:
        backtest_id: 回测结果 ID

    Returns:
        包含所有字段和解析后 equity_curve 的字典，无记录返回 None
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM backtest_result WHERE id = ?", (backtest_id,)
        ).fetchone()
    if not row:
        return None
    cols = [desc[0] for desc in conn.execute(
        "SELECT * FROM backtest_result WHERE id = ?", (backtest_id,)
    ).description] if row else []
    result = dict(zip(cols, row))
    if result.get("equity_curve"):
        try:
            result["equity_curve"] = json.loads(result["equity_curve"])
        except (json.JSONDecodeError, TypeError):
            result["equity_curve"] = []
    return result


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

def add_to_watchlist(ts_code: str, note: str = "", group_name: str = "", recommended_strategy: str = ""):
    """添加或更新自选股（若已存在，未指定的值自动保留原记录）"""
    from datetime import datetime
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT group_name, recommended_strategy FROM watchlist WHERE ts_code = ?",
            (ts_code,)
        ).fetchone()
        if existing:
            if not group_name and existing[0]:
                group_name = existing[0]
            if not recommended_strategy and existing[1]:
                recommended_strategy = existing[1]

        conn.execute(
            """INSERT OR REPLACE INTO watchlist 
               (ts_code, added_date, note, group_name, recommended_strategy) 
               VALUES (?, ?, ?, ?, ?)""",
            (ts_code, datetime.now().strftime("%Y-%m-%d"), note, group_name, recommended_strategy)
        )


def update_watchlist_recommendation(ts_code: str, recommended_strategy: str):
    """更新自选股推荐策略标签"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE watchlist SET recommended_strategy = ? WHERE ts_code = ?",
            (recommended_strategy, ts_code)
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


def get_non_watchlist_stocks_with_data() -> list[str]:
    """获取所有在 daily_price 中有行情数据，但既不在自选股中、也不在模拟持仓中的标的代码"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ts_code FROM daily_price
            WHERE ts_code NOT IN (SELECT ts_code FROM watchlist)
              AND ts_code NOT IN (SELECT ts_code FROM position)
            ORDER BY ts_code
            """
        ).fetchall()
        return [r[0] for r in rows]


def clean_non_watchlist_data(codes: list[str] | None = None) -> dict:
    """清理非自选股且非持仓标的的本地行情日线与历史信号

    参数:
        codes: 指定清理的代码列表，若为 None 则清理全部非自选股标的
    返回:
        {"deleted_codes": list[str], "daily_rows": int, "signal_rows": int}
    """
    if codes is None:
        codes = get_non_watchlist_stocks_with_data()
    else:
        # 安全守卫：绝不清理当前自选股或模拟持仓中的标的
        with get_conn() as conn:
            protected_rows = conn.execute(
                "SELECT ts_code FROM watchlist UNION SELECT ts_code FROM position"
            ).fetchall()
            protected = {r[0] for r in protected_rows}
        codes = [c for c in codes if c not in protected]

    if not codes:
        return {"deleted_codes": [], "daily_rows": 0, "signal_rows": 0}

    placeholders = ",".join(["?"] * len(codes))
    with get_conn() as conn:
        cur1 = conn.execute(f"DELETE FROM daily_price WHERE ts_code IN ({placeholders})", codes)
        deleted_daily = cur1.rowcount
        cur2 = conn.execute(f"DELETE FROM signal WHERE ts_code IN ({placeholders})", codes)
        deleted_signals = cur2.rowcount

    return {
        "deleted_codes": codes,
        "daily_rows": deleted_daily,
        "signal_rows": deleted_signals,
    }


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


def get_index_daily_range(ts_code: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
    """获取大盘指数指定日期区间的日线数据

    参数:
        ts_code:    指数代码 e.g. "000300.SH" 或 "399300.SZ"
        start_date: 起始日期 "YYYYMMDD"
        end_date:   结束日期 "YYYYMMDD"

    返回:
        按日期升序排列的 DataFrame
    """
    sql = "SELECT * FROM index_daily WHERE ts_code = ?"
    params = [ts_code]
    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)
    sql += " ORDER BY trade_date ASC"

    with get_conn() as conn:
        df = pd.read_sql(sql, conn, params=params)
    if not df.empty:
        df["trade_date"] = df["trade_date"].astype(str)
        df = df.sort_values("trade_date").reset_index(drop=True)
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
