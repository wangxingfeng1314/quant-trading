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

from app.theme import inject_theme_css, inject_page_transition_js

# ============================================================
# 登录认证（可选，通过 APP_AUTH_ENABLED 环境变量启用）
# ============================================================
from core.config import APP_AUTH_ENABLED, APP_AUTH_PASSWORD

if APP_AUTH_ENABLED:
    # 检查是否已登录
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        inject_theme_css()
        st.title("🔐 A股量化交易系统 - 登录")
        st.markdown("---")

        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            password = st.text_input("请输入密码", type="password", key="auth_pwd")
            if st.button("登录", type="primary", width="stretch"):
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
    from core.config import SCHEDULER_HOUR, SCHEDULER_MINUTE
    schedule_str = f"{SCHEDULER_HOUR:02d}:{SCHEDULER_MINUTE:02d}"
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", "QuantTrading-DataUpdate", "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "下次运行时间" in line or "状态" in line:
                    return f"⏰ {line.strip()}"
            return f"⏰ 定时任务: {schedule_str} 每日执行"
        return "⏰ 定时任务: 未配置"
    except Exception:
        return f"⏰ 定时任务: {schedule_str} 每日执行"

# ============================================================
# 页面路由表与 URL 映射定义
# ============================================================
PAGE_DEFINITIONS = [
    {
        "slug": "dashboard",
        "label": "🏠 首页看板",
        "module": "app.dashboard",
        "aliases": ["dashboard", "home", "kanban", "看板", "首页", "0", "🏠 首页看板", "首页看板"],
    },
    {
        "slug": "data",
        "label": "📈 数据浏览",
        "module": "app.data_viewer",
        "aliases": ["data", "data_viewer", "kline", "数据", "行情", "1", "📈 数据浏览", "数据浏览"],
    },
    {
        "slug": "backtest",
        "label": "🔬 回测中心",
        "module": "app.backtest",
        "aliases": ["backtest", "bt", "回测", "2", "🔬 回测中心", "回测中心"],
    },
    {
        "slug": "strategy",
        "label": "📚 策略百科",
        "module": "app.strategy_intro",
        "aliases": ["strategy", "strategy_intro", "strategies", "百科", "策略", "3", "📚 策略百科", "策略百科"],
    },
    {
        "slug": "screener",
        "label": "🔍 选股筛选",
        "module": "app.screener",
        "aliases": ["screener", "filter", "选股", "筛选", "4", "🔍 选股筛选", "选股筛选"],
    },
    {
        "slug": "signal",
        "label": "📡 信号中心",
        "module": "app.signal",
        "aliases": ["signal", "signals", "信号", "5", "📡 信号中心", "信号中心"],
    },
    {
        "slug": "portfolio",
        "label": "💼 持仓管理",
        "module": "app.portfolio",
        "aliases": ["portfolio", "positions", "holdings", "持仓", "账户", "6", "💼 持仓管理", "持仓管理"],
    },
]

NAV_ITEMS = [item["label"] for item in PAGE_DEFINITIONS]
LABEL_TO_SLUG = {item["label"]: item["slug"] for item in PAGE_DEFINITIONS}
SLUG_TO_LABEL = {item["slug"]: item["label"] for item in PAGE_DEFINITIONS}
PAGES = {item["label"]: item["module"] for item in PAGE_DEFINITIONS}

ALIAS_TO_LABEL = {}
for item in PAGE_DEFINITIONS:
    lbl = item["label"]
    for alias in item["aliases"]:
        ALIAS_TO_LABEL[alias.lower().strip()] = lbl

# ------------------------------------------------------------
# URL 参数与侧边栏导航双向同步 (支持 F5 刷新、浏览器前进/后退、直接链接访问)
# ------------------------------------------------------------
raw_param = ""
if "page" in st.query_params:
    raw_val = st.query_params.get("page")
    if isinstance(raw_val, (list, tuple)):
        raw_param = str(raw_val[0]).strip().lower() if raw_val else ""
    else:
        raw_param = str(raw_val).strip().lower()

target_from_url = ALIAS_TO_LABEL.get(raw_param)

# 若首次访问或 URL 参数外部变化（例如 F5 刷新或浏览器前进/后退），同步至 session_state
if "main_nav_radio" not in st.session_state:
    st.session_state["main_nav_radio"] = target_from_url or NAV_ITEMS[0]
elif target_from_url and st.session_state.get("_last_synced_url_param") != raw_param:
    st.session_state["main_nav_radio"] = target_from_url

def _on_nav_change():
    selected = st.session_state.get("main_nav_radio")
    if selected in LABEL_TO_SLUG:
        slug = LABEL_TO_SLUG[selected]
        st.query_params["page"] = slug
        st.session_state["_last_synced_url_param"] = slug

st.sidebar.title("📊 A股量化交易系统")
st.sidebar.markdown("---")

# 侧边栏单选导航（使用 index=None 配合 key="main_nav_radio" 实现平滑双向绑定）
page = st.sidebar.radio(
    "导航",
    NAV_ITEMS,
    index=None,
    key="main_nav_radio",
    on_change=_on_nav_change,
)
if not page or page not in PAGES:
    page = NAV_ITEMS[0]

# 兜底确保 URL query_params 始终与当前激活页面一致
active_slug = LABEL_TO_SLUG.get(page, "dashboard")
st.session_state["_last_synced_url_param"] = active_slug
if st.query_params.get("page") != active_slug:
    st.query_params["page"] = active_slug

# ============================================================
# 主区域：渲染当前页面模块（顶部即为页面标题，无多余占位 div）
# ============================================================
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
    f"版本: **v0.4.5**"
)

# 登出按钮（仅认证模式下显示）
if APP_AUTH_ENABLED:
    if st.sidebar.button("🚪 登出", width="stretch"):
        st.session_state["authenticated"] = False
        st.rerun()

# 定时任务状态（缓存结果，避免每次切换都调用 schtasks 造成卡顿）
st.sidebar.caption(_cached_schtasks_caption())

# 一键更新数据按钮
st.sidebar.markdown("---")
st.sidebar.markdown("**🔄 数据维护**")
if st.sidebar.button("🔄 更新自选股数据", type="primary", width="stretch"):
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
                st.sidebar.warning("⏳ 另一个更新任务正在运行中")
                if st.sidebar.button("🔓 强制重置更新锁", key="main_reset_lock", width="stretch"):
                    from data.storage import force_release_update_lock
                    force_release_update_lock()
                    st.sidebar.success("锁已重置，请重试")
                    st.rerun()
            else:
                from scripts.init_data import run_update
                run_update(days=14, watchlist=True, progress_callback=_on_progress)
                progress_bar.empty()
                status_text.empty()
                st.sidebar.success(f"✅ 数据更新完成 ({datetime.now().strftime('%H:%M')})")
                st.rerun()
    except Exception as e:
        progress_bar.empty()
        status_text.empty()
        st.sidebar.error(f"❌ 更新失败: {e}")

# 数据库备份/恢复（使用 SQLite 原生在线热备 API，保障 WAL 模式一致性与文件锁安全）
from core.config import DB_PATH
from data.storage import (backup_database, restore_database,
                          get_non_watchlist_stocks_with_data, clean_non_watchlist_data,
                          get_instrument_list)
backup_dir = DB_PATH.parent / "backups"
backup_dir.mkdir(exist_ok=True)

if st.sidebar.button("💾 备份数据库", width="stretch"):
    backup_name = f"quant_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
    backup_path = backup_dir / backup_name
    try:
        backup_database(backup_path)
        st.sidebar.success(f"✅ 已安全热备: {backup_name}")
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
                restore_database(backup_dir / selected)
                st.success(f"✅ 已恢复: {selected}")
                st.rerun()
            except Exception as e:
                st.error(f"❌ 恢复失败: {e}")

# 清理非自选股的历史缓存数据
try:
    non_wl_cached = get_non_watchlist_stocks_with_data()
    if non_wl_cached:
        inst_df = get_instrument_list()
        name_map = dict(zip(inst_df["ts_code"], inst_df["name"])) if not inst_df.empty else {}
        with st.sidebar.popover(f"🧹 清理非自选缓存 ({len(non_wl_cached)}只)"):
            st.markdown(f"**检测到 {len(non_wl_cached)} 只已移出自选的标的存有本地历史K线：**")
            display_items = [f"`{c}` {name_map.get(c, '')}" for c in non_wl_cached]
            st.caption("、".join(display_items))
            st.info("💡 清理后可减少数据库体积，回测与信号扫描多选框默认不再显示这些已删标的。持仓标的受安全保护绝不误删。")
            confirm_clean = st.checkbox("确认彻底清理这部分历史K线与信号", key="clean_non_wl_confirm")
            if st.button("🗑️ 立即彻底清除", type="secondary", disabled=not confirm_clean, key="btn_clean_non_wl", width="stretch"):
                try:
                    res = clean_non_watchlist_data()
                    st.cache_data.clear()
                    st.success(f"✅ 清理完成！共清除 {len(res['deleted_codes'])} 只标的，删除 {res['daily_rows']} 条日线数据")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ 清理失败: {e}")
except Exception:
    pass

# ============================================================
# 全局主题与切页过渡注入（挂载于侧边栏最底部，绝对不占据侧边栏或主区顶部空间）
# ============================================================
with st.sidebar:
    inject_theme_css()
    inject_page_transition_js()
