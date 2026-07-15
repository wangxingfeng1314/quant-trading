"""初始化/更新数据库 - 数据获取命令行工具

本脚本提供两种运行模式：
  1. 完整初始化模式: 首次部署时使用，创建表结构 + 下载股票列表 + 批量拉取日线 + 指数数据
  2. 增量更新模式:   每日定时任务使用，只拉取最近N天的数据，跳过已是最新的股票

用法:
    # 首次初始化（下载沪深300 + 前300只股票的全部历史数据）
    python scripts/init_data.py --stocks 300

    # 自选股模式（只下载自选股的日线数据）
    python scripts/init_data.py --stocks 5000 --watchlist

    # 增量更新（只补最近30天，适合每日运行）
    python scripts/init_data.py --update --days 30

    # 自选股增量更新（只更新自选股）
    python scripts/init_data.py --update --days 30 --watchlist

    # 断点续传（跳过已有数据的股票）
    python scripts/init_data.py --stocks 300 --resume
"""
import sys              # 系统参数（用于 argv 模拟）
import time             # 休眠控制（限速）
import random           # 随机数（请求间隔抖动）
import logging          # 日志
import logging.handlers # 日志轮转处理器
from pathlib import Path  # 路径处理

# ---------- 项目路径设置 ----------
# 当前文件路径: scripts/init_data.py
# 项目根目录: 父目录的父目录
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))  # 确保能 import 项目模块

# ---------- 导入数据模块 ----------
from core.config import DATA_START_DATE                     # 数据起始日期
from data.storage import init_db, save_stock_list, save_daily, get_stock_list, get_latest_date, get_watchlist
from data.fetcher import fetch_stock_list, fetch_daily, fetch_index_components, check_data_freshness, update_all_indices, INDEX_CODES, fetch_index_daily
from data.cleaner import clean_daily                        # 数据清洗
from data.storage import save_index_daily                   # 指数数据存储

# ---------- 日志配置 ----------
# 同时输出到控制台(StreamHandler)和日志文件(RotatingFileHandler)
# 日志文件每 5MB 轮转一次，保留 3 份备份
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            PROJECT_ROOT / "logs" / "init_data.log",
            maxBytes=5*1024*1024,   # 5MB
            backupCount=3,          # 保留3份历史日志
            encoding="utf-8",
        ),
    ]
)
logger = logging.getLogger(__name__)

# 进度回调（供 UI 展示下载进度用）
_progress_callback = None


def set_progress_callback(callback):
    """设置进度回调函数 callback(current, total, ts_code, name)"""
    global _progress_callback
    _progress_callback = callback


def run_update(days: int = 5, watchlist: bool = False, progress_callback=None):
    """以编程方式运行增量更新（供调度器调用，避免修改 sys.argv）

    参数:
        days: 拉取最近 N 天的数据，默认 5 天
        watchlist: 是否只更新自选股，默认 False
        progress_callback: 进度回调(current, total, ts_code, name)

    用法:
        from scripts.init_data import run_update
        run_update(days=5)
        run_update(days=5, watchlist=True)  # 只更新自选股
    """
    global _progress_callback
    _progress_callback = progress_callback
    # 模拟命令行参数，调用 main() 执行更新
    import sys
    argv = ["init_data.py", "--update", "--days", str(days)]
    if watchlist:
        argv.append("--watchlist")
    sys.argv = argv
    try:
        main()
    finally:
        # 清理 sys.argv，避免影响其他模块的 argparse 调用
        sys.argv = sys.argv[:1]


def main():
    """主函数：解析命令行参数并执行初始化/更新操作"""
    import argparse
    parser = argparse.ArgumentParser(description="初始化/更新A股量化数据库")

    # ----- 命令行参数定义 -----
    parser.add_argument("--stocks", type=int, default=300,
                        help="下载股票数量 (默认300)")
    parser.add_argument("--start", type=str, default=DATA_START_DATE,
                        help=f"数据起始日期 (默认{ DATA_START_DATE })")
    parser.add_argument("--resume", action="store_true",
                        help="断点续传：跳过已有数据的股票")
    parser.add_argument("--update", action="store_true",
                        help="增量更新模式：只补最近N天的数据")
    parser.add_argument("--days", type=int, default=30,
                        help="增量更新时拉取最近N天的数据 (默认30)")
    parser.add_argument("--watchlist", action="store_true",
                        help="自选股模式：只下载/更新自选股列表中的股票")
    args = parser.parse_args()

    from datetime import datetime, timedelta
    end_date = datetime.now().strftime("%Y%m%d")  # 结束日期统一为今天


    # ================================================================
    # 模式一：增量更新模式
    #   适用场景：每日收盘后运行，补足最新N天的数据
    #   特点：
    #     - 跳过已是最新数据的股票（减少请求量）
    #     - 每只请求前随机休眠 0.3~1.5s（避免AKShare限流）
    #     - 最后同步更新大盘指数数据
    # ================================================================
    if args.update:
        # 检查数据库是否已有数据
        freshness = check_data_freshness()
        if not freshness["latest_date"]:
            logger.error("数据库为空，请先运行完整初始化: python scripts/init_data.py --stocks 300")
            sys.exit(1)

        logger.info("=" * 50)
        logger.info(f"增量更新模式: 补足最近 {args.days} 天数据")
        logger.info(f"当前最新数据: {freshness['latest_date']}")
        logger.info(f"有数据的股票: {freshness['active_stocks']} 只")

        # 计算拉取区间的起始日期 = 今天 - 增量天数
        start_dt = datetime.now() - timedelta(days=args.days)
        start_date = start_dt.strftime("%Y%m%d")
        logger.info(f"拉取范围: {start_date} ~ {end_date}")

        # 从数据库获取所有股票列表
        stock_df = get_stock_list()
        if stock_df.empty:
            logger.error("数据库中无股票列表，请先运行完整初始化")
            sys.exit(1)

        # 自选股模式：只更新自选股
        if args.watchlist:
            watchlist_df = get_watchlist()
            if watchlist_df.empty:
                logger.warning("自选股列表为空，将更新所有股票")
            else:
                stock_df = stock_df[stock_df["ts_code"].isin(watchlist_df["ts_code"])]
                logger.info(f"自选股模式: 只更新 {len(stock_df)} 只自选股")

        # 批量查询每只股票的最新日期（一次SQL查完，比逐条查快100倍）
        from data.storage import get_conn
        with get_conn() as conn:
            latest_map = dict(conn.execute(
                "SELECT ts_code, MAX(trade_date) FROM daily_price GROUP BY ts_code"
            ).fetchall())

        # 统计变量
        success_count = 0  # 成功更新数
        skip_count = 0     # 跳过数（已是最新）
        fail_count = 0     # 失败数
        total = len(stock_df)

        # ---------- 遍历每只股票拉取增量数据 ----------
        for i, (_, row) in enumerate(stock_df.iterrows(), 1):
            ts_code = row["ts_code"]  # 股票代码 e.g. "000001.SZ"
            name = row["name"]        # 股票名称 e.g. "平安银行"

            # 进度回调
            if _progress_callback:
                _progress_callback(i, total, ts_code, name)

            # 最近14天（含今天）的数据每次都重新拉取，确保最完整
            skip_threshold = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
            latest = latest_map.get(ts_code, "")
            if latest and latest < skip_threshold:
                # 最新数据超过14天前 → 数据太旧不用刷新，跳过
                skip_count += 1
                continue

            # 限速：每只请求前随机休眠 0.3~1.5 秒
            # AKShare免费API有频率限制，随机休眠可有效降低被限流的概率
            time.sleep(random.uniform(0.3, 1.5))

            try:
                # 从14天前开始拉取，覆盖最近14天的数据
                fetch_start = skip_threshold
                # 调用数据源级联获取（AKShare → Tushare → Baostock）
                df = fetch_daily(ts_code, fetch_start, end_date)
                if df.empty:
                    continue    # 所有数据源均无数据，跳过

                # 数据清洗（OHLC验证、停牌处理、去重等）
                df = clean_daily(df)
                if df.empty:
                    continue

                # 写入数据库（INSERT OR REPLACE，安全重复执行）
                save_daily(df)
                success_count += 1

                # 每50只打印一次进度
                if i % 50 == 0:
                    logger.info(f"[{i}/{total}] 已更新 {success_count} 只 (跳过{skip_count}, 失败{fail_count})")

            except Exception as e:
                fail_count += 1
                logger.warning(f"[{i}/{total}] {ts_code} {name} 更新失败: {e}")

        logger.info("=" * 50)
        logger.info(f"增量更新完成！成功: {success_count}, 失败: {fail_count}")

        # ---------- 同步更新大盘指数数据 ----------
        logger.info("同步更新大盘指数数据...")
        try:
            idx_count = update_all_indices(start_date, end_date)
            logger.info(f"指数更新完成，共 {idx_count} 条")
        except Exception as e:
            logger.warning(f"指数更新失败: {e}")

        logger.info(f"建议将此命令加入每日定时任务")
        return  # 增量更新模式结束


    # ================================================================
    # 模式二：完整初始化模式
    #   适用场景：首次部署或重建数据库
    #   步骤：
    #     1. 初始化数据库表结构
    #     2. 获取A股股票列表（含行业信息）
    #     3. 获取沪深300成分股作为默认股票池
    #     4. 批量下载日线数据
    #     5. 下载大盘指数数据
    # ================================================================

    # ---------- Step 1: 创建表结构 ----------
    logger.info("=" * 50)
    logger.info("Step 1: 初始化数据库表结构")
    init_db()  # 建表（幂等操作，安全重复执行）
    logger.info("数据库表创建完成")

    # ---------- Step 2: 获取股票列表 ----------
    logger.info("=" * 50)
    logger.info("Step 2: 获取A股股票列表")
    stock_df = fetch_stock_list()  # 多源级联获取
    if stock_df.empty:
        logger.error("获取股票列表失败，请检查网络连接或AKShare是否正常")
        sys.exit(1)
    logger.info(f"共获取 {len(stock_df)} 只股票")

    # ---------- Step 3: 确定要下载的股票池 ----------
    logger.info("=" * 50)
    if args.watchlist:
        # 自选股模式：只下载自选股数据
        watchlist_df = get_watchlist()
        if watchlist_df.empty:
            logger.error("自选股列表为空，请先添加自选股")
            sys.exit(1)
        target_stocks = stock_df[stock_df["ts_code"].isin(watchlist_df["ts_code"])]
        logger.info(f"Step 3: 自选股模式，共 {len(target_stocks)} 只股票")
    else:
        logger.info("Step 3: 获取沪深300成分股")
        hs300_codes = fetch_index_components("399300.SZ")  # 获取沪深300成分股
        if not hs300_codes:
            # 获取成分股失败时，直接取前N只股票
            logger.warning("获取沪深300成分股失败，使用前300只股票")
            hs300_codes = stock_df["ts_code"].head(args.stocks).tolist()
        else:
            logger.info(f"沪深300成分股: {len(hs300_codes)} 只")
            # 如果成分股数量不足指定数量，用剩余股票补齐
            if len(hs300_codes) < args.stocks:
                extra = stock_df[~stock_df["ts_code"].isin(hs300_codes)]["ts_code"].head(
                    args.stocks - len(hs300_codes)).tolist()
                hs300_codes.extend(extra)

        # 从股票列表中筛选出目标股票
        target_stocks = stock_df[stock_df["ts_code"].isin(hs300_codes[:args.stocks])]

    save_stock_list(stock_df)  # 保存完整股票列表到数据库
    logger.info(f"目标下载: {len(target_stocks)} 只股票")

    # ---------- Step 4: 批量下载日线数据 ----------
    logger.info("=" * 50)
    logger.info(f"Step 4: 下载日线数据 ({args.start} ~ {end_date})")
    success_count = 0
    fail_count = 0
    total = len(target_stocks)

    for i, (_, row) in enumerate(target_stocks.iterrows(), 1):
        ts_code = row["ts_code"]
        name = row["name"]

        # 断点续传：如果股票已有最新数据则跳过
        if args.resume:
            latest = get_latest_date(ts_code)
            if latest and latest >= end_date:
                logger.debug(f"[{i}/{total}] {ts_code} {name} 数据已是最新，跳过")
                success_count += 1
                continue

        try:
            # 拉取该股票的完整历史日线
            df = fetch_daily(ts_code, args.start, end_date)
            if df.empty:
                logger.warning(f"[{i}/{total}] {ts_code} {name} 无数据")
                fail_count += 1
                continue

            # 数据清洗
            df = clean_daily(df)
            if not df.empty:
                save_daily(df)  # 存入数据库
                success_count += 1
                if len(df) > 0:
                    logger.info(f"[{i}/{total}] {ts_code} {name} "
                                f"=> {len(df)} 条日线数据")
            else:
                fail_count += 1
                logger.warning(f"[{i}/{total}] {ts_code} {name} 清洗后无数据")

            # 每50只暂停3秒，防止触发API限流
            if i % 50 == 0:
                logger.info(f"--- 已处理 {i}/{total}，暂停3秒 ---")
                time.sleep(3)

        except Exception as e:
            fail_count += 1
            logger.error(f"[{i}/{total}] {ts_code} {name} 下载失败: {e}")
            time.sleep(1)

    logger.info("=" * 50)
    logger.info(f"数据下载完成！成功: {success_count}, 失败: {fail_count}")
    logger.info(f"数据库路径: {PROJECT_ROOT / 'data' / 'quant.db'}")

    # ---------- Step 5: 下载大盘指数数据 ----------
    logger.info("=" * 50)
    logger.info("Step 5: 下载大盘指数数据")
    try:
        idx_count = update_all_indices(args.start, end_date)
        logger.info(f"指数数据: {idx_count} 条")
    except Exception as e:
        logger.warning(f"指数数据下载失败: {e}")

    # ---------- 使用提示 ----------
    logger.info("=" * 50)
    logger.info("💡 每日更新建议:")
    logger.info("   将以下命令加入定时任务(如Windows任务计划):")
    logger.info(f"   cd {PROJECT_ROOT} && python scripts/init_data.py --update --days 30")


if __name__ == "__main__":
    main()  # 脚本入口
