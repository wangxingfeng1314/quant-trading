"""数据服务 - 封装数据更新、查询、维护逻辑"""
import logging
from datetime import datetime
from typing import Optional

from data.storage import (
    get_daily, get_stock_list, get_etf_list, get_instrument_list,
    get_daily_count, get_latest_date, get_stocks_with_data,
    save_daily, save_stock_list, save_etf_list, save_index_daily,
    get_index_daily, check_db_integrity, init_db,
    acquire_update_lock, release_update_lock,
)
from data.fetcher import fetch_daily, fetch_stock_list, fetch_etf_list, fetch_index_daily
from data.cleaner import clean_daily
from services.validators import validate_stock_code, validate_date

logger = logging.getLogger(__name__)


class DataService:
    """数据服务 - 封装数据层的业务逻辑

    用法:
        svc = DataService()
        svc.update_stock_data("000001.SZ")
        df = svc.get_daily_data("000001.SZ", "20230101", "20240101")
    """

    @staticmethod
    def get_daily_data(ts_code: str, start_date: str = "", end_date: str = "",
                       limit: int = 0):
        """查询单只股票的日线数据

        Args:
            ts_code: 股票代码
            start_date: 开始日期 YYYYMMDD（可选）
            end_date: 结束日期 YYYYMMDD（可选）
            limit: 返回最近 N 条（0=全部）

        Returns:
            DataFrame，按日期升序排列
        """
        validate_stock_code(ts_code)
        if start_date:
            validate_date(start_date)
        if end_date:
            validate_date(end_date)
        return get_daily(ts_code, start_date, end_date, limit)

    @staticmethod
    def get_instrument_list():
        """获取所有可交易标的列表（股票 + ETF）"""
        return get_instrument_list()

    @staticmethod
    def get_stocks_with_data(min_days: int = 60):
        """获取有足够日线数据的股票列表"""
        return get_stocks_with_data(min_days)

    @staticmethod
    def get_db_stats() -> dict:
        """获取数据库统计信息"""
        return {
            "daily_count": get_daily_count(),
            "stocks_with_data": len(get_stocks_with_data()),
            "integrity": check_db_integrity(),
        }

    @staticmethod
    def update_stock_data(ts_code: str, days: int = 14) -> bool:
        """更新单只股票的日线数据

        Args:
            ts_code: 股票代码
            days: 获取最近 N 天的数据

        Returns:
            True=成功, False=失败
        """
        validate_stock_code(ts_code)
        try:
            from datetime import timedelta
            from data.fetcher import fetch_instrument_daily
            latest = get_latest_date(ts_code)
            end_date = datetime.now().strftime("%Y%m%d")
            if latest:
                start_date = latest
            else:
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

            df = fetch_instrument_daily(ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = clean_daily(df)
                save_daily(df)
                logger.info(f"更新 {ts_code}: +{len(df)} 条")
                return True
            return False
        except Exception as e:
            logger.error(f"更新 {ts_code} 失败: {e}")
            return False

    @staticmethod
    def update_watchlist_data(days: int = 14, progress_callback=None) -> dict:
        """更新自选股数据

        Args:
            days: 获取最近 N 天
            progress_callback: 回调函数(current, total, ts_code, name)

        Returns:
            {"success": int, "failed": int, "total": int}
        """
        from data.storage import get_watchlist
        watchlist = get_watchlist()
        if watchlist.empty:
            return {"success": 0, "failed": 0, "total": 0}

        codes = watchlist["ts_code"].tolist()
        total = len(codes)
        success = 0
        failed = 0

        if not acquire_update_lock():
            logger.warning("无法获取更新锁，可能有其他更新正在进行")
            return {"success": 0, "failed": 0, "total": total}

        try:
            for i, ts_code in enumerate(codes):
                ok = DataService.update_stock_data(ts_code, days)
                if ok:
                    success += 1
                else:
                    failed += 1

                if progress_callback:
                    name = watchlist[watchlist["ts_code"] == ts_code].iloc[0].get("note", "")
                    progress_callback(i + 1, total, ts_code, name)
        finally:
            release_update_lock()

        return {"success": success, "failed": failed, "total": total}

    @staticmethod
    def update_stock_list():
        """更新股票列表"""
        try:
            df = fetch_stock_list()
            if df is not None and not df.empty:
                save_stock_list(df)
                logger.info(f"股票列表更新: {len(df)} 只")
                return len(df)
        except Exception as e:
            logger.error(f"更新股票列表失败: {type(e).__name__}")
        return 0

    @staticmethod
    def update_index_data():
        """更新大盘指数数据"""
        try:
            for code in ["000001.SH", "399001.SZ", "399006.SZ"]:
                df = fetch_index_daily(code)
                if df is not None and not df.empty:
                    save_index_daily(df)
                    logger.info(f"指数 {code} 更新: {len(df)} 条")
        except Exception as e:
            logger.error(f"更新指数数据失败: {type(e).__name__}")
