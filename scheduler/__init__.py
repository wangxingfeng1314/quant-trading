"""A股量化交易系统 - 定时任务调度器

使用 APScheduler 实现每日定时数据更新。
支持两种运行模式:
  1. 独立服务: python -m scheduler.service
  2. 内嵌到主程序: 在 run.py 中 import scheduler 并调用 start()

流程:
  数据更新（加写锁）→ 信号扫描 → 推送通知
"""
import logging
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


def update_data_job():
    """执行数据更新任务（由 APScheduler 定时触发）

    完整流程:
      1. 尝试获取更新锁（防与 Windows 计划任务冲突）
      2. 增量更新数据（日线 + 指数）
      3. 扫描自选股信号
      4. 推送信号通知到配置的通道
    """
    logger.info("=" * 50)
    logger.info("定时任务触发：开始增量更新数据...")
    logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # 延迟导入，避免循环依赖
        from scripts.init_data import run_update
        from data.storage import update_lock, get_watchlist
        from engine.scanner import scan_signals
        from notifier.push import notify_signals, notify_position_summary

        with update_lock(timeout=300):
            # ---------- Step 1: 更新数据 ----------
            run_update(days=5, watchlist=True)
            logger.info("定时任务完成：数据更新成功")

            # ---------- Step 2: 扫描自选股信号 ----------
            watchlist = get_watchlist()
            if not watchlist.empty:
                stocks = watchlist["ts_code"].tolist()
                logger.info(f"开始扫描 {len(stocks)} 只自选股信号...")
                signals = scan_signals(universe=stocks, save=True)
                logger.info(f"信号扫描完成：共 {len(signals)} 条信号")

                # ---------- Step 3: 推送通知 ----------
                if signals:
                    notify_signals(signals)
                    logger.info(f"已推送 {len(signals)} 条信号通知")
            else:
                logger.info("自选股为空，跳过信号扫描")

            # ---------- Step 4: 推送持仓盈亏日报 ----------
            notify_position_summary()
            logger.info("持仓盈亏日报已推送")

    except Exception as e:
        logger.error(f"定时任务失败: {e}", exc_info=True)
    logger.info("=" * 50)


def start():
    """启动后台定时调度器"""
    global _scheduler

    if not SCHEDULER_ENABLED:
        logger.info("定时任务已禁用 (SCHEDULER_ENABLED=false)")
        return

    if _scheduler is not None and _scheduler.running:
        logger.warning("调度器已在运行中，跳过重复启动")
        return

    # 配置日志
    log_file = LOG_DIR / "scheduler.log"
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    logging.getLogger().addHandler(fh)

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
    """停止调度器"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度器已停止")


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