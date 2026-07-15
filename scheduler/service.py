"""A股量化交易系统 - 定时调度独立服务入口

用法:
    python -m scheduler.service          # 前台运行（按 Ctrl+C 停止）
    python -m scheduler.service --daemon  # 后台运行（Windows 用 start /B）
"""
import sys
import time
import logging
import signal
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import LOG_DIR, SCHEDULER_HOUR, SCHEDULER_MINUTE

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "scheduler_service.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("scheduler_service")


def main():
    from scheduler import start, stop, get_status

    # 注册优雅退出
    shutdown_flag = False

    def _signal_handler(signum, frame):
        nonlocal shutdown_flag
        if shutdown_flag:
            return
        shutdown_flag = True
        logger.info(f"收到信号 {signum}，正在停止调度器...")
        stop()
        logger.info("调度器已停止，服务退出")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # 启动调度器
    logger.info("=" * 50)
    logger.info("A股量化交易系统 - 定时调度服务")
    logger.info(f"数据更新时段: 每日 {SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d}")
    logger.info("=" * 50)

    start()

    # 打印状态
    status = get_status()
    if status["running"]:
        logger.info("调度器运行中...")
        for j in status["jobs"]:
            logger.info(f"  [{j['id']}] {j['name']} | 下次执行: {j['next_run_time']}")
    else:
        logger.warning("调度器未启动 (SCHEDULER_ENABLED=false 或启动失败)")

    # 保持主进程运行
    try:
        while not shutdown_flag:
            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        if not shutdown_flag:
            logger.info("正在停止调度器...")
            stop()
            logger.info("调度器已停止，服务退出")


if __name__ == "__main__":
    main()