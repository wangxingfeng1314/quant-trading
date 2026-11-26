"""A股量化交易系统 - 定时任务调度器

使用 APScheduler 实现每日定时数据更新。
支持两种运行模式:
  1. 独立服务: python -m scheduler.service
  2. 内嵌到主程序: 在 run.py 中 import scheduler 并调用 start()

流程:
  数据更新（加写锁）→ 信号扫描 → 推送通知
"""
import logging
import logging.handlers
import sys
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

from core.config import (
    SCHEDULER_ENABLED,
    SCHEDULER_HOUR,
    SCHEDULER_MINUTE,
    PROJECT_ROOT,
    LOG_DIR,
)

logger = logging.getLogger(__name__)

# 全局调度器（单例）
_scheduler: BackgroundScheduler | None = None
_LOG_HANDLER: logging.handlers.RotatingFileHandler | None = None


def scan_and_notify(force: bool = False):
    """扫描自选股信号并推送通知（数据更新完成后调用）

    供 APScheduler（update_data_job）与 Windows 计划任务
    （run_scheduled_update.py）两个入口复用，保证闭环一致。

    Windows 计划任务与 APScheduler 是两套独立调度，文件锁只串行化写入、
    不去重推送，双入口同时部署会把信号日报与持仓日报各推两遍。这里用
    notify_log 表的「日期 + 任务」主键做当日幂等，重复调度直接跳过。

    流程:
      1. 扫描自选股信号（写入数据库）
      2. 有信号则推送信号通知
      3. 推送持仓盈亏日报
      4. 登记当日已推送标记
    """
    from data.storage import get_watchlist, notification_already_sent, mark_notification_sent
    from engine.scanner import scan_signals
    from notifier.push import notify_signals, notify_position_summary

    job_key = f"scan_and_notify:{datetime.now().strftime('%Y%m%d')}"
    if not force:
        try:
            if notification_already_sent(job_key):
                logger.info(f"当日扫描推送已执行过（{job_key}），跳过重复调度")
                return
        except Exception as e:
            # notify_log 表尚未建立（旧库未迁移）时降级为「不去重」，不能阻断推送
            logger.warning(f"推送幂等检查不可用，降级为单次执行: {e}")
            job_key = None
    else:
        job_key = None

    # ---------- 1. 按自选分组隔离扫描信号并推送 ----------
    scan_ok = False
    try:
        from strategies import STRATEGY_REGISTRY
        watchlist = get_watchlist()
        if not watchlist.empty:
            has_group = "group_name" in watchlist.columns
            has_rec = "recommended_strategy" in watchlist.columns
            rotation_stocks, regular_stocks = [], []
            for _, row in watchlist.iterrows():
                code = str(row["ts_code"]).strip()
                if not code:
                    continue
                grp = str(row["group_name"]).strip() if has_group and row.get("group_name") is not None else ""
                rec = str(row["recommended_strategy"]).strip() if has_rec and row.get("recommended_strategy") is not None else ""
                if grp == "ETF轮动池" or "动量轮动" in rec or "momentum_rotation" in rec.lower():
                    rotation_stocks.append(code)
                else:
                    regular_stocks.append(code)

            # 若未显式配置 ETF轮动池（至少2只），自动探测本地已下载的多资产轮动预设 ETF（>=60根K线）
            if len(rotation_stocks) < 2:
                try:
                    from data.storage import get_stocks_with_data
                    from strategies.momentum_rotation import ROTATION_ETF_PRESETS
                    ready_set = set(get_stocks_with_data(min_days=60))
                    preset_ready = [c for c, _ in ROTATION_ETF_PRESETS if c in ready_set]
                    if len(preset_ready) >= 2:
                        rotation_stocks = preset_ready
                        rot_set = set(rotation_stocks)
                        # 将已在轮动池中的标的从常规个股策略池剔除，避免重复互搏推送
                        regular_stocks = [c for c in regular_stocks if c not in rot_set]
                except Exception:
                    pass

            # 若轮动池仍不足2只，并入常规扫描以避免横截面空跑
            if len(rotation_stocks) == 1:
                regular_stocks.extend(rotation_stocks)
                rotation_stocks = []

            single_strats = [
                n for n, cls in STRATEGY_REGISTRY.items()
                if not getattr(cls, "is_cross_sectional", False)
            ]
            signals = []
            if regular_stocks:
                logger.info(f"开始扫描 {len(regular_stocks)} 只常规自选股信号...")
                signals.extend(scan_signals(
                    universe=regular_stocks,
                    strategy_names=single_strats or None,
                    save=True,
                    force_refresh=True,
                    respect_market_regime=True,
                    respect_watchlist_strategy=True,
                    anti_whipsaw=True,
                ))
            if len(rotation_stocks) >= 2 and "momentum_rotation" in STRATEGY_REGISTRY:
                logger.info(f"开始独立扫描 {len(rotation_stocks)} 只 ETF 轮动池...")
                signals.extend(scan_signals(
                    universe=rotation_stocks,
                    strategy_names=["momentum_rotation"],
                    save=True,
                    force_refresh=True,
                    respect_market_regime=False,
                    respect_watchlist_strategy=False,
                    anti_whipsaw=True,
                ))

            signals.sort(key=lambda s: s.score, reverse=True)
            logger.info(f"信号扫描完成：共 {len(signals)} 条信号")

            # 推送信号通知（定时日报自动跳过已持仓买入，防重复追买）
            if signals:
                try:
                    notify_signals(signals, skip_held_buys=True)
                except TypeError:
                    notify_signals(signals)
                logger.info(f"已推送 {len(signals)} 条信号通知")
            else:
                logger.info("当日经大盘择时/适配策略/防洗盘过滤后无有效交易信号")
        else:
            logger.info("自选股为空，跳过信号扫描")
        scan_ok = True
    except Exception as e:
        logger.error(f"自选股信号扫描/推送发生异常: {e}", exc_info=True)

    # ---------- 2. 推送持仓盈亏与风控预警日报 ----------
    pos_ok = False
    try:
        pushed = notify_position_summary()
        if pushed:
            logger.info("持仓风控日报已推送（含ATR止损/移动止盈/保本预警检查）")
        else:
            logger.info("本次未推送持仓日报（当前模拟持仓为空或推送失败）")
        pos_ok = True
    except Exception as e:
        logger.error(f"持仓盈亏日报推送发生异常: {e}", exc_info=True)

    # 仅在扫描与推送均未抛出异常时登记幂等键；否则保留重试机会，防止瞬时故障锁死当日通知
    if job_key and scan_ok and pos_ok:
        try:
            mark_notification_sent(job_key)
        except Exception as e:
            logger.warning(f"登记推送幂等标记失败（不影响本次推送）: {e}")


def update_data_job():
    """执行数据更新任务（由 APScheduler 定时触发）

    完整流程:
      1. 尝试获取更新锁（防与 Windows 计划任务冲突）
      2. 增量更新数据（日线 + 指数）
      3. 扫描自选股信号 + 推送通知（复用 scan_and_notify）
    """
    logger.info("=" * 50)
    logger.info("定时任务触发：开始增量更新数据...")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # 延迟导入，避免循环依赖
        from scripts.init_data import run_update
        from data.storage import update_lock

        with update_lock(timeout=300) as acquired:
            if not acquired:
                logger.warning("未能获取数据库更新文件锁（超时300s），可能有其他更新任务正在运行，安全跳过本次调度")
                return

            # ---------- Step 1: 更新数据 ----------
            run_update(days=5, watchlist=True)
            logger.info("定时任务完成：数据更新成功")

            # ---------- Step 1.5: 自动补齐缺失估值缓存与新增自选推荐策略 ----------
            try:
                from services.valuation_service import refresh_watchlist_valuations_if_stale
                from services.strategy_recommender import batch_update_watchlist_recommendations
                refresh_watchlist_valuations_if_stale(max_age_days=7)
                batch_update_watchlist_recommendations(force=False)
            except Exception as sync_err:
                logger.warning(f"定时估值/策略标签自动补齐跳过: {sync_err}")

            # ---------- Step 2: 扫描信号 + 推送通知 ----------
            scan_and_notify()

    except Exception as e:
        logger.error(f"定时任务失败: {e}", exc_info=True)
    logger.info("=" * 50)


def start():
    """启动后台定时调度器"""
    global _scheduler, _LOG_HANDLER

    if not SCHEDULER_ENABLED:
        logger.info("定时任务已禁用 (SCHEDULER_ENABLED=false)")
        return

    if _scheduler is not None and _scheduler.running:
        logger.warning("调度器已在运行中，跳过重复启动")
        return

    # 配置日志：start()/stop() 可反复调用，handler 必须去重，否则同一条日志
    # 会被写多份，文件句柄也会随热重载不断累积
    if _LOG_HANDLER is None:
        log_file = LOG_DIR / "scheduler.log"
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        logging.getLogger().addHandler(fh)
        _LOG_HANDLER = fh

    # 创建后台调度器
    _scheduler = BackgroundScheduler(
        timezone="Asia/Shanghai",
        job_defaults={
            "coalesce": True,           # 错过执行时合并为一次
            "max_instances": 1,         # 同一时间只运行一个实例
            "misfire_grace_time": 3600,  # 错过执行后 1 小时内仍补跑
        },
    )

    # 注册每日数据更新任务
    _scheduler.add_job(
        update_data_job,
        trigger=CronTrigger(
            hour=SCHEDULER_HOUR,
            minute=SCHEDULER_MINUTE,
            timezone="Asia/Shanghai",
        ),
        id="daily_data_update",
        name="每日数据更新",
        replace_existing=True,
    )

    # 添加执行结果监听
    def _job_listener(event):
        if event.exception:
            logger.error(f"任务 [{event.job_id}] 执行异常: {event.exception}")
        else:
            logger.info(f"任务 [{event.job_id}] 执行成功 (已耗时 {event.scheduled_run_time})")

    _scheduler.add_listener(
        _job_listener,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
    )

    _scheduler.start()
    logger.info(
        f"定时调度器已启动，每日 {SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d} 执行数据更新"
    )


def stop():
    """停止调度器并移除本模块挂载的日志 handler"""
    global _scheduler, _LOG_HANDLER
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度器已停止")
    if _LOG_HANDLER is not None:
        logging.getLogger().removeHandler(_LOG_HANDLER)
        _LOG_HANDLER.close()
        _LOG_HANDLER = None


def get_status() -> dict:
    """获取调度器运行状态"""
    global _scheduler
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = [
        {
            "id": j.id,
            "name": j.name,
            "next_run_time": str(j.next_run_time) if j.next_run_time else None,
            "trigger": str(j.trigger),
        }
        for j in _scheduler.get_jobs()
    ]
    return {"running": _scheduler.running, "jobs": jobs}