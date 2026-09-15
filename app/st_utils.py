"""Streamlit 中文组件工具 - 替换内置组件的英文文本"""
from datetime import date, datetime
import streamlit as st
import pandas as pd


def chinese_date_input(label: str, default_start: date = None,
                       default_end: date = None, key: str = None) -> tuple:
    """中文日期范围 — 快捷范围按钮 + 年月日数字输入，无日历控件，绝无英文

    优化：顶部提供「近1月/近3月/近半年/近1年/全部」快捷按钮，
    点击自动设置下方年月日输入框并刷新，无需手动逐年月日。
    """
    from datetime import timedelta

    if default_start is None:
        default_start = date(date.today().year - 2, 1, 1)
    if default_end is None:
        default_end = date.today()

    st.markdown(f"**📅 {label}**")

    # ---------- 快捷范围按钮 ----------
    # 点击后写入年月日输入框的 session_state 并 rerun
    from core.config import DATA_START_DATE
    today = date.today()
    all_start = datetime.strptime(DATA_START_DATE, "%Y%m%d").date()
    presets = {
        "近1月": (today - timedelta(days=30), today),
        "近3月": (today - timedelta(days=90), today),
        "近半年": (today - timedelta(days=180), today),
        "近1年": (today - timedelta(days=365), today),
        "全部": (all_start, today),
    }
    pc = st.columns(len(presets))
    for col, (label_p, (s, e)) in zip(pc, presets.items()):
        with col:
            if st.button(label_p, key=f"{key}_preset_{label_p}",
                         use_container_width=True):
                st.session_state[f"{key}_sy"] = s.year
                st.session_state[f"{key}_sm"] = s.month
                st.session_state[f"{key}_sd"] = s.day
                st.session_state[f"{key}_ey"] = e.year
                st.session_state[f"{key}_em"] = e.month
                st.session_state[f"{key}_ed"] = e.day
                st.rerun()

    # ---------- 开始日期 ----------
    st.caption("开始日期")
    c1, c2, c3 = st.columns(3)
    with c1:
        sy = st.number_input("年", min_value=2019, max_value=2030, step=1,
                             value=default_start.year, key=f"{key}_sy", label_visibility="collapsed")
    with c2:
        sm = st.number_input("月", min_value=1, max_value=12, step=1,
                             value=default_start.month, key=f"{key}_sm", label_visibility="collapsed")
    with c3:
        sd = st.number_input("日", min_value=1, max_value=31, step=1,
                             value=default_start.day, key=f"{key}_sd", label_visibility="collapsed")

    # ---------- 结束日期 ----------
    st.caption("结束日期")
    c4, c5, c6 = st.columns(3)
    with c4:
        ey = st.number_input("年", min_value=2019, max_value=2030, step=1,
                             value=default_end.year, key=f"{key}_ey", label_visibility="collapsed")
    with c5:
        em = st.number_input("月", min_value=1, max_value=12, step=1,
                             value=default_end.month, key=f"{key}_em", label_visibility="collapsed")
    with c6:
        ed = st.number_input("日", min_value=1, max_value=31, step=1,
                             value=default_end.day, key=f"{key}_ed", label_visibility="collapsed")

    s = f"{int(sy):04d}{int(sm):02d}{int(sd):02d}"
    e = f"{int(ey):04d}{int(em):02d}{int(ed):02d}"
    return s, e


def chinese_date_picker(label: str, default_val: date = None,
                        key: str = None) -> str:
    """中文单日期 — 3个数字输入框，无日历控件，绝无英文"""
    if default_val is None:
        default_val = date.today()

    st.markdown(f"**📅 {label}**")
    c1, c2, c3 = st.columns(3)
    with c1:
        y = st.number_input("年", min_value=2019, max_value=2030,
                            value=default_val.year, key=key, label_visibility="collapsed")
    with c2:
        m = st.number_input("月", min_value=1, max_value=12,
                            value=default_val.month, key=f"{key}_m", label_visibility="collapsed")
    with c3:
        d = st.number_input("日", min_value=1, max_value=31,
                            value=default_val.day, key=f"{key}_d", label_visibility="collapsed")

    return f"{int(y):04d}{int(m):02d}{int(d):02d}"


def chinese_dataframe(df: pd.DataFrame, height: int = 400):
    """中文数据表格（替代 st.dataframe 的英文 AG Grid）— 深色金融主题"""
    table_html = df.to_html(index=False, escape=False, na_rep="-")

    html = f"""
    <div style="overflow-x: auto; overflow-y: auto; height: {height}px; border: 1px solid #1e2a44; border-radius: 10px; background-color: #0d1526;">
        <style>
            table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
            th {{ background-color: #131c30; color: #8b98b8; padding: 8px 12px; text-align: left !important;
                 position: sticky; top: 0; z-index: 1; border-bottom: 2px solid #2a3a5e; font-weight: 600; }}
            td {{ padding: 6px 12px; border-bottom: 1px solid #1e2a44; color: #dbe2ee; background-color: #0d1526; }}
            tr:hover td {{ background-color: #182338; }}
        </style>
        {table_html}
    </div>
    <div style="text-align: right; font-size: 12px; color: #8b98b8; margin-top: 4px;">
        共 {len(df)} 条记录
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def chinese_metric_grid(metrics: dict, columns: int = 4):
    """中文指标卡片网格

    Args:
        metrics: {"标签": "值", ...}
        columns: 每行显示几个
    """
    cols = st.columns(columns)
    for i, (label, value) in enumerate(metrics.items()):
        with cols[i % columns]:
            st.metric(label=label, value=value)


# 策略风格 → 图标/颜色映射
STYLE_META = {
    "短线": ("⚡", "#ef5350"),
    "震荡": ("↔️", "#ffd54f"),
    "中长线": ("📈", "#4fc3f7"),
    "综合": ("🧩", "#ab47bc"),
}


def strategy_label(name: str, desc: str, style: str = "综合") -> str:
    """策略下拉标签：带风格图标与名称，如 "⚡[短线] kdj_cross - KDJ金叉死叉" """
    icon, _ = STYLE_META.get(style, ("📌", "#8b98b8"))
    return f"{icon}[{style}] {name} - {desc}"


def strategy_style_badge(style: str) -> str:
    """策略风格徽章 HTML（用于卡片展示）"""
    icon, color = STYLE_META.get(style, ("📌", "#8b98b8"))
    return (f'<span style="background:{color}22;color:{color};border:1px solid '
            f'{color}55;border-radius:6px;padding:1px 8px;font-size:0.8rem;'
            f'margin-right:6px;">{icon}{style}</span>')


# ============================================================
# UI 级数据高频读取缓存（减少 40万行日线全表扫描与重跑耗时）
# ============================================================

@st.cache_data(ttl=300)
def cached_get_stocks_with_data(min_days: int = 60) -> list:
    """缓存版获取有日线数据的标的代码（缓存 5 分钟）"""
    from data.storage import get_stocks_with_data
    return get_stocks_with_data(min_days)


@st.cache_data(ttl=120)
def cached_check_data_freshness() -> dict:
    """缓存版数据时效检查（缓存 2 分钟）"""
    from data.fetcher import check_data_freshness
    return check_data_freshness()


@st.cache_data(ttl=300)
def cached_instrument_list() -> pd.DataFrame:
    """缓存版全标的列表（股票 + ETF，缓存 5 分钟）"""
    from data.storage import get_instrument_list
    return get_instrument_list()

