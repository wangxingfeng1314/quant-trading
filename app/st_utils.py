"""Streamlit 中文组件工具 - 替换内置组件的英文文本"""
from datetime import date, datetime
import streamlit as st
import pandas as pd


def chinese_date_input(label: str, default_start: date = None,
                       default_end: date = None, key: str = None) -> tuple:
    """中文日期范围 — 纯数字输入，无日历控件，绝无英文"""
    if default_start is None:
        default_start = date(date.today().year - 2, 1, 1)
    if default_end is None:
        default_end = date.today()

    st.markdown(f"**📅 {label}**")

    # 开始日期
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

    # 结束日期
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
    """中文数据表格（替代 st.dataframe 的英文 AG Grid）"""
    table_html = df.to_html(index=False, escape=False, na_rep="-")

    html = f"""
    <div style="overflow-x: auto; overflow-y: auto; height: {height}px; border: 1px solid #ddd; border-radius: 4px; background-color: #FFFFFF;">
        <style>
            table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
            th {{ background-color: #f0f2f6; color: #000000; padding: 8px 12px; text-align: left !important;
                 position: sticky; top: 0; z-index: 1; border-bottom: 2px solid #ddd; }}
            td {{ padding: 6px 12px; border-bottom: 1px solid #eee; color: #000000; background-color: #FFFFFF; }}
            tr:hover td {{ background-color: #f5f5f5; }}
        </style>
        {table_html}
    </div>
    <div style="text-align: right; font-size: 12px; color: #666; margin-top: 4px;">
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
