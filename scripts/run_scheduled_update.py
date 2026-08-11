"""定时任务入口：增量更新自选股数据（由 Windows 计划任务调用）。

更新完成后自动扫描自选股信号并推送通知，与 APScheduler
（scheduler.update_data_job）保持同一闭环逻辑。

等价于旧版 update_data.bat 内嵌的 python -c 多行代码，但作为独立脚本
可避免 cmd.exe 对跨行引号参数的解析问题（LF/CRLF 均不可靠）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.storage import update_lock
from scripts.init_data import run_update
from scheduler import scan_and_notify

if __name__ == "__main__":
    with update_lock(timeout=300):
        # 1. 增量更新自选股数据
        run_update(days=3, watchlist=True)
        # 2. 扫描自选股信号 + 推送通知（含持仓盈亏日报）
        scan_and_notify()
