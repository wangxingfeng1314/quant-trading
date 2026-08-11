"""信号服务 - 封装信号扫描、查询、推送逻辑"""
import logging
from typing import Optional, List

from engine.scanner import scan_signals
from data.storage import get_signals, save_signals_batch
from notifier.push import notify_signals, send_notification
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class SignalService:
    """信号服务 - 封装信号扫描和查询的业务逻辑

    用法:
        svc = SignalService()
        signals = svc.scan(["000001.SZ"], ["ma_cross"], "20240101")
        history = svc.get_history(strategy="ma_cross", limit=50)
    """

    @staticmethod
    def scan(universe: list = None, strategy_names: list = None,
             end_date: str = "", save: bool = True,
             progress_callback=None, parallel: bool = True) -> list:
        """扫描信号

        Args:
            universe: 股票列表（None=全部有数据的股票）
            strategy_names: 策略名列表（None=全部策略）
            end_date: 扫描日期 YYYYMMDD
            save: 是否保存到数据库
            progress_callback: 进度回调
            parallel: 是否并行扫描

        Returns:
            Signal 列表，按 score 降序
        """
        return scan_signals(
            universe=universe,
            strategy_names=strategy_names,
            end_date=end_date,
            save=save,
            progress_callback=progress_callback,
            parallel=parallel,
        )

    @staticmethod
    def get_history(trade_date: str = "", strategy: str = "",
                    limit: int = 100):
        """查询历史信号"""
        return get_signals(trade_date=trade_date, strategy=strategy, limit=limit)

    @staticmethod
    def notify(signals: list, strategy_names: list = None):
        """推送信号通知"""
        if not signals:
            return False
        notify_signals(signals, strategy_names)
        return True

    @staticmethod
    def batch_save(signals: list):
        """批量保存信号"""
        if not signals:
            return 0
        save_signals_batch(signals)
        return len(signals)
