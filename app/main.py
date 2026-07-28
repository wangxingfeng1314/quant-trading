"""Streamlit 主入口"""
import streamlit as st
import app  # noqa: F401
import traceback
from datetime import datetime

st.set_page_config(
    page_title="A股量化交易系统",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("📊 A股量化交易系统")
st.sidebar.markdown("---")

# 启动时确保数据库表结构是最新的（含迁移）
from data.storage import init_db
init_db()

# 导航
page = st.sidebar.radio(
    "导航",
    ["🏠 首页看板", "📈 数据浏览", "🔬 回测中心", "📚 策略百科", "🔍 选股筛选", "📡 信号中心", "💼 持仓管理"],
    index=0,
)

st.sidebar.markdown("---")

# 数据库状态
from data.storage import get_daily_count, get_watchlist, check_db_integrity
db_count = get_daily_count()
watchlist = get_watchlist()
watchlist_count = len(watchlist)

# 数据库完整性检查
db_health = check_db_integrity()
if not db_health["ok"]:
    st.sidebar.error(f"🔴 **数据库异常**: {db_health['message']}")
else:
    st.sidebar.success(f"🟢 {db_health['message']}")

st.sidebar.markdown(
    f"**系统状态**\n\n"
    f"⭐ 自选股: {watchlist_count} 只\n\n"
    f"📊 日线数据: {db_count:,} 条\n\n"
    f"版本: **v0.3.0**"
)

# 定时任务状态
import subprocess
try:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", "QuantTrading-DataUpdate", "/fo", "LIST", "/v"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        for line in result.stdout.split("\n"):
            if "下次运行时间" in line or "状态" in line:
                st.sidebar.caption(f"⏰ {line.strip()}")
                break
        else:
            st.sidebar.caption("⏰ 定时任务: 17:00 每日执行")
    else:
        st.sidebar.caption("⏰ 定时任务: 未配置")
except Exception:
    st.sidebar.caption("⏰ 定时任务: 17:00 每日执行")

# 一键更新数据按钮
st.sidebar.markdown("---")
st.sidebar.markdown("**🔄 数据维护**")
if st.sidebar.button("🔄 更新自选股数据", type="primary", use_container_width=True):
    progress_bar = st.sidebar.progress(0)
    status_text = st.sidebar.empty()

    def _on_progress(current, total, ts_code, name):
        progress_bar.progress(current / total)
        status_text.text(f"⏳ [{current}/{total}] {name} ({ts_code})")

    try:
        from scripts.init_data import run_update, set_progress_callback
        set_progress_callback(_on_progress)
        run_update(days=14, watchlist=True)
        progress_bar.empty()
        status_text.empty()
        st.sidebar.success(f"✅ 数据更新完成 ({datetime.now().strftime('%H:%M')})")
        st.rerun()
    except Exception as e:
        progress_bar.empty()
        status_text.empty()
        st.sidebar.error(f"❌ 更新失败: {e}")

# 数据库备份/恢复
from core.config import DB_PATH
import shutil
backup_dir = DB_PATH.parent / "backups"
backup_dir.mkdir(exist_ok=True)

if st.sidebar.button("💾 备份数据库", use_container_width=True):
    backup_name = f"quant_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
    backup_path = backup_dir / backup_name
    try:
        shutil.copy2(DB_PATH, backup_path)
        st.sidebar.success(f"✅ 已备份: {backup_name}")
    except Exception as e:
        st.sidebar.error(f"❌ 备份失败: {e}")

# 列出最近备份
backups = sorted(backup_dir.glob("*.db"), reverse=True)
if backups:
    with st.sidebar.popover("🔄 恢复备份"):
        selected = st.selectbox("选择备份文件",
                                [b.name for b in backups[:10]],
                                key="restore_select")
        if st.button("⚠️ 恢复此备份", type="secondary"):
            try:
                shutil.copy2(backup_dir / selected, DB_PATH)
                st.success(f"✅ 已恢复: {selected}")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 恢复失败: {e}")

# 页面路由（带错误隔离）
PAGES = {
    "🏠 首页看板": "app.dashboard",
    "📈 数据浏览": "app.data_viewer",
    "🔬 回测中心": "app.backtest",
    "📚 策略百科": "app.strategy_intro",
    "🔍 选股筛选": "app.screener",
    "📡 信号中心": "app.signal",
    "💼 持仓管理": "app.portfolio",
}

if page in PAGES:
    try:
        module = __import__(PAGES[page], fromlist=["show"])
        module.show()
    except Exception as e:
        st.error(f"🚨 页面加载失败: {e}")
        with st.expander("查看错误详情"):
            st.code(traceback.format_exc())
