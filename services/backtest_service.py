"""回测服务 - 封装回测执行、结果查询、参数优化逻辑"""
import logging
from typing import Optional, List

from engine.backtester import Backtester, grid_search, grid_search_parallel
from data.storage import (
    get_backtest_results, get_backtest_trades, get_backtest_result_by_id,
    save_backtest_result, save_backtest_trades,
)
from core.config import DEFAULT_CAPITAL
from core.exceptions import InvalidParamsError, ValidationError
from services.validators import (
    validate_stock_code, validate_date, validate_date_range,
    validate_capital, validate_strategy_name,
)

logger = logging.getLogger(__name__)


class BacktestService:
    """回测服务 - 封装回测业务逻辑

    用法:
        svc = BacktestService()
        result = svc.run_backtest("ma_cross", {"fast": 5, "slow": 20},
                                   ["000001.SZ"], "20230101", "20240101")
    """

    @staticmethod
    def run_backtest(strategy_name: str, params: dict, universe: list,
                     start_date: str, end_date: str,
                     capital: float = None, save: bool = True,
                     execution_mode: str = "current_close",
                     risk_manager = None,
                     max_active_positions: int = 0):
        """执行单次回测

        Args:
            strategy_name: 策略名称
            params: 策略参数字典
            universe: 股票代码列表
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            capital: 初始资金（默认用 DEFAULT_CAPITAL）
            save: 是否保存到数据库
            execution_mode: 撮合模式 "current_close" 或 "next_open"
            risk_manager: 独立风控拦截器 RiskManager 实例
            max_active_positions: 最大持仓只数控制

        Returns:
            BacktestResult 对象

        Raises:
            ValidationError: 输入校验失败
            InvalidParamsError: 策略参数无效
        """
        from strategies import STRATEGY_REGISTRY

        # 输入校验
        validate_strategy_name(strategy_name, STRATEGY_REGISTRY)
        validate_date_range(start_date, end_date)
        capital = validate_capital(capital or DEFAULT_CAPITAL)

        if not universe:
            raise ValidationError("股票列表不能为空", field="universe")
        for code in universe:
            validate_stock_code(code)

        strategy_cls = STRATEGY_REGISTRY[strategy_name]

        bt = Backtester(
            strategy_cls=strategy_cls,
            params=params,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=capital,
            execution_mode=execution_mode,
            risk_manager=risk_manager,
            max_active_positions=max_active_positions,
        )
        return bt.run(save=save)

    @staticmethod
    def run_grid_search(strategy_name: str, param_grid: dict, universe: list,
                        start_date: str, end_date: str,
                        capital: float = None, metric: str = "total_return",
                        parallel: bool = False, progress_callback=None):
        """参数网格搜索

        Args:
            strategy_name: 策略名称
            param_grid: {参数名: [取值列表]}
            universe: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            capital: 初始资金
            metric: 排序指标
            parallel: 是否并行
            progress_callback: 进度回调

        Returns:
            排序后的结果列表
        """
        from strategies import STRATEGY_REGISTRY

        validate_strategy_name(strategy_name, STRATEGY_REGISTRY)
        validate_date_range(start_date, end_date)
        capital = validate_capital(capital or DEFAULT_CAPITAL)

        strategy_cls = STRATEGY_REGISTRY[strategy_name]

        if parallel:
            return grid_search_parallel(
                strategy_cls=strategy_cls, universe=universe,
                start_date=start_date, end_date=end_date,
                initial_capital=capital, param_grid=param_grid,
                metric=metric, progress_callback=progress_callback,
            )
        else:
            return grid_search(
                strategy_cls=strategy_cls, universe=universe,
                start_date=start_date, end_date=end_date,
                initial_capital=capital, param_grid=param_grid,
                metric=metric, progress_callback=progress_callback,
            )

    @staticmethod
    def get_history(limit: int = 20):
        """获取历史回测记录"""
        return get_backtest_results(limit=limit)

    @staticmethod
    def get_trades(backtest_id: int):
        """获取某次回测的交易明细"""
        return get_backtest_trades(backtest_id)

    @staticmethod
    def get_result(backtest_id: int) -> Optional[dict]:
        """获取单条回测结果（含解析后的权益曲线）"""
        return get_backtest_result_by_id(backtest_id)

    @staticmethod
    def get_results_for_compare(backtest_ids: list) -> list:
        """获取多条回测结果用于对比

        Args:
            backtest_ids: 回测 ID 列表

        Returns:
            回测结果字典列表（含解析后的 equity_curve）
        """
        results = []
        for bt_id in backtest_ids:
            r = get_backtest_result_by_id(bt_id)
            if r:
                results.append(r)
        return results
