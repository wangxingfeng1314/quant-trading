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

    @staticmethod
    def arbitrate(signals: list, min_net_threshold: float = 0.15) -> list:
        """多策略同标的信号仲裁与多空净额结算"""
        return resolve_signal_conflicts(signals, min_net_threshold)


def resolve_signal_conflicts(signals: List, min_net_threshold: float = 0.15) -> list:
    """多策略同标的信号仲裁与多空净额结算

    功能:
        1. 多策略同向共振: 增强置信度得分，合并多策略触发原因。
        2. 多策略多空冲突: 计算多空净额 Net Score = sum(Buy) - sum(Sell)。
           分歧过大(差值 < min_net_threshold)予以对冲消除；明确偏向一方时输出净方向。
        3. 单策略独有信号: 保持原样输出。

    Args:
        signals: 原始 Signal 列表
        min_net_threshold: 多空博弈判定有效净额的最小差值阈值

    Returns:
        仲裁后的无冲突 Signal 列表，按 score 降序
    """
    from core.models import Signal
    if not signals:
        return []

    grouped = {}
    for s in signals:
        grouped.setdefault(s.ts_code, []).append(s)

    arbitrated = []
    for ts_code, sig_list in grouped.items():
        if len(sig_list) == 1:
            arbitrated.append(sig_list[0])
            continue

        trade_date = sig_list[0].trade_date
        price_ref = next((s.price_ref for s in sig_list if s.price_ref > 0), 0.0)

        buys = [s for s in sig_list if s.direction == "BUY"]
        sells = [s for s in sig_list if s.direction == "SELL"]

        # 1. 全部同向（共振增信）
        if buys and not sells:
            strategies = [s.strategy for s in buys]
            avg_score = sum(s.score for s in buys) / len(buys)
            boosted_score = min(1.0, avg_score + 0.08 * (len(buys) - 1))
            reasons = "; ".join(f"{s.strategy}: {s.reason}" for s in buys if s.reason)
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=f"ensemble_{len(buys)}",
                direction="BUY",
                score=round(boosted_score, 4),
                reason=f"[{len(buys)}策略多头共振: {', '.join(strategies)}] {reasons}",
                price_ref=price_ref,
                context_snapshot={
                    "is_ensemble": True,
                    "strategies": strategies,
                    "count": len(buys),
                }
            ))
        elif sells and not buys:
            strategies = [s.strategy for s in sells]
            avg_score = sum(s.score for s in sells) / len(sells)
            boosted_score = min(1.0, avg_score + 0.08 * (len(sells) - 1))
            reasons = "; ".join(f"{s.strategy}: {s.reason}" for s in sells if s.reason)
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy=f"ensemble_{len(sells)}",
                direction="SELL",
                score=round(boosted_score, 4),
                reason=f"[{len(sells)}策略空头共振: {', '.join(strategies)}] {reasons}",
                price_ref=price_ref,
                context_snapshot={
                    "is_ensemble": True,
                    "strategies": strategies,
                    "count": len(sells),
                }
            ))
        else:
            # 2. 多空冲突对冲
            buy_score = sum(s.score for s in buys)
            sell_score = sum(s.score for s in sells)
            diff = buy_score - sell_score

            if abs(diff) < min_net_threshold:
                # 多空分歧极大，相互抵消，不产生交易
                continue

            if diff > 0:
                net_dir = "BUY"
                net_score = min(1.0, max(0.1, diff / len(sig_list) + 0.4))
                reason = f"[多空博弈净胜: {len(buys)}买vs{len(sells)}卖] 多头占优 (净分 {diff:+.2f})"
            else:
                net_dir = "SELL"
                net_score = min(1.0, max(0.1, abs(diff) / len(sig_list) + 0.4))
                reason = f"[多空博弈净胜: {len(sells)}卖vs{len(buys)}买] 空头占优 (净分 {diff:+.2f})"

            strategies = [f"{s.strategy}({s.direction})" for s in sig_list]
            arbitrated.append(Signal(
                ts_code=ts_code,
                trade_date=trade_date,
                strategy="arbitrated_net",
                direction=net_dir,
                score=round(net_score, 4),
                reason=reason,
                price_ref=price_ref,
                context_snapshot={
                    "is_arbitrated": True,
                    "strategies": strategies,
                    "buy_score": round(buy_score, 4),
                    "sell_score": round(sell_score, 4),
                }
            ))

    arbitrated.sort(key=lambda s: s.score, reverse=True)
    return arbitrated
