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

# 注入全局深色金融主题与即时切页监听
from app.theme import inject_theme_css, inject_page_transition_js
inject_theme_css()
inject_page_transition_js()

# ============================================================
# 登录认证（可选，通过 APP_AUTH_ENABLED 环境变量启用）
# ============================================================
from core.config import APP_AUTH_ENABLED, APP_AUTH_PASSWORD

if APP_AUTH_ENABLED:
    # 检查是否已登录
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        st.title("🔐 A股量化交易系统 - 登录")
        st.markdown("---")

        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            password = st.text_input("请输入密码", type="password", key="auth_pwd")
            if st.button("登录", type="primary", use_container_width=True):
                expected = APP_AUTH_PASSWORD or "quant123"
                if password == expected:
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("❌ 密码错误")
            st.caption("💡 默认密码: quant123（请在 .env 中设置 APP_AUTH_PASSWORD 修改）")
        st.stop()

# 启动时确保数据库表结构是最新的（进程单例，切页无需反复执行 DDL）
from data.storage import init_db, get_daily_count, get_watchlist, check_db_integrity

@st.cache_resource
def _ensure_db_initialized():
    init_db()
    return True

_ensure_db_initialized()

# ------------------------------------------------------------
# 缓存：减少每次切换页面的重跑开销（DB 概览 + 定时任务状态）
# ------------------------------------------------------------
@st.cache_data(ttl=180)
def _cached_db_overview():
    return get_daily_count(), len(get_watchlist()), check_db_integrity()

@st.cache_data(ttl=600)
def _cached_schtasks_caption():
    import subprocess
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", "QuantTrading-DataUpdate", "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "下次运行时间" in line or "状态" in line:
                    return f"⏰ {line.strip()}"
            return "⏰ 定时任务: 17:00 每日执行"
        return "⏰ 定时任务: 未配置"
    except Exception:
        return "⏰ 定时任务: 17:00 每日执行"

# 页面路由表
PAGES = {
    "🏠 首页看板": "app.dashboard",
    "📈 数据浏览": "app.data_viewer",
    "🔬 回测中心": "app.backtest",
    "📚 策略百科": "app.strategy_intro",
    "🔍 选股筛选": "app.screener",
    "📡 信号中心": "app.signal",
    "💼 持仓管理": "app.portfolio",
}

st.sidebar.title("📊 A股量化交易系统")
st.sidebar.markdown("---")

# 导航
page = st.sidebar.radio(
    "导航",
    ["🏠 首页看板", "📈 数据浏览", "🔬 回测中心", "📚 策略百科", "🔍 选股筛选", "📡 信号中心", "💼 持仓管理"],
    index=0,
    key="main_nav_radio",
)

# ============================================================
# 主区域：由前端 JS 在 0ms 瞬间抹除上一个页面并拉起过渡遮罩，
# Python 渲染完成后由观察器平滑渐入，避免旧画面残留
# ============================================================
page_slot = st.empty()
with page_slot.container():
    try:
        module = __import__(PAGES[page], fromlist=["show"])
        module.show()
    except Exception as e:
        st.error(f"🚨 页面加载失败: {e}")
        with st.expander("查看错误详情"):
            st.code(traceback.format_exc())

# ============================================================
# 侧边栏其余状态（独立于主区域，主页面渲染后再填充）
# ============================================================
st.sidebar.markdown("---")

# 数据库状态（缓存 30s，减少每次切换的重跑开销）
db_count, watchlist_count, db_health = _cached_db_overview()
if not db_health["ok"]:
    st.sidebar.error(f"🔴 **数据库异常**: {db_health['message']}")
else:
    st.sidebar.success(f"🟢 {db_health['message']}")

st.sidebar.markdown(
    f"**系统状态**\n\n"
    f"⭐ 自选股: {watchlist_count} 只\n\n"
    f"📊 日线数据: {db_count:,} 条\n\n"
    f"版本: **v0.4.0**"
)

# 登出按钮（仅认证模式下显示）
if APP_AUTH_ENABLED:
    if st.sidebar.button("🚪 登出", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

# 定时任务状态（缓存结果，避免每次切换都调用 schtasks 造成卡顿）
st.sidebar.caption(_cached_schtasks_caption())

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
        from data.storage import update_lock
        with update_lock(timeout=10) as acquired:
            if not acquired:
                progress_bar.empty()
                status_text.empty()
                st.sidebar.warning("⏳ 另一个更新任务正在运行中，请稍后再试")
            else:
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
        st.warning("⚠️ 恢复将覆盖当前数据库，此操作不可撤销！")
        confirm = st.checkbox("我确认要恢复", key="restore_confirm")
        if st.button("⚠️ 恢复此备份", type="secondary", disabled=not confirm):
            try:
                shutil.copy2(backup_dir / selected, DB_PATH)
                st.success(f"✅ 已恢复: {selected}")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 恢复失败: {e}")
