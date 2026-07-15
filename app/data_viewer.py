"""数据浏览页面 - K线图 + 成交量 + 指标"""
import app  # noqa: F401 (ensure project root in path)
from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from data.storage import get_stock_list, get_daily, get_daily_count, get_stocks_with_data
from app.st_utils import chinese_dataframe, chinese_date_input
from data.indicators import apply_indicators
from data.cleaner import clean_daily


def create_candlestick_chart(df: pd.DataFrame, ts_code: str, name: str,
                             show_volume: bool = True,
                             show_ma: bool = True,
                             show_macd: bool = False,
                             show_boll: bool = False) -> go.Figure:
    """创建K线图（Plotly）"""
    # 确定子图数量
    rows = 1
    row_heights = [0.6]
    subplot_titles = [f"{name} ({ts_code})"]

    if show_volume:
        rows += 1
        row_heights.append(0.15)
        subplot_titles.append("成交量")

    if show_macd:
        rows += 1
        row_heights.append(0.15)
        subplot_titles.append("MACD")

    # 归一化高度
    total = sum(row_heights)
    row_heights = [h / total for h in row_heights]

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )

    # K线
    fig.add_trace(go.Candlestick(
        x=df["trade_date"],
        open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing_line_color="red",
        decreasing_line_color="green",
        increasing_fillcolor="red",
        decreasing_fillcolor="green",
        name="K线",
    ), row=1, col=1)

    # 均线
    if show_ma and "ma5" in df.columns:
        for col, color in [("ma5", "#FF6B6B"), ("ma10", "#4ECDC4"),
                            ("ma20", "#45B7D1"), ("ma60", "#96CEB4")]:
            if col in df.columns:
                fig.add_trace(go.Scatter(
                    x=df["trade_date"], y=df[col],
                    mode="lines", name=col.upper(),
                    line=dict(width=1, color=color),
                ), row=1, col=1)

    # 布林带
    if show_boll and "boll_upper" in df.columns:
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_upper"],
            mode="lines", name="BOLL上轨",
            line=dict(width=1, color="gray", dash="dash"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_mid"],
            mode="lines", name="BOLL中轨",
            line=dict(width=1, color="gray"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_lower"],
            mode="lines", name="BOLL下轨",
            line=dict(width=1, color="gray", dash="dash"),
            fill="tonexty", fillcolor="rgba(128,128,128,0.1)",
        ), row=1, col=1)

    current_row = 1

    # 成交量
    if show_volume:
        current_row += 1
        colors = ["red" if c >= o else "green"
                  for c, o in zip(df["close"], df["open"])]
        fig.add_trace(go.Bar(
            x=df["trade_date"], y=df["volume"],
            marker_color=colors, name="成交量",
            showlegend=False,
        ), row=current_row, col=1)

        # 成交量均线
        if "vol_ma5" in df.columns:
            fig.add_trace(go.Scatter(
                x=df["trade_date"], y=df["vol_ma5"],
                mode="lines", name="VOL_MA5",
                line=dict(width=1, color="orange"),
            ), row=current_row, col=1)

    # MACD
    if show_macd and "dif" in df.columns:
        current_row += 1
        macd_colors = ["red" if v >= 0 else "green" for v in df["macd_hist"]]
        fig.add_trace(go.Bar(
            x=df["trade_date"], y=df["macd_hist"],
            marker_color=macd_colors, name="MACD柱",
            showlegend=False,
        ), row=current_row, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["dif"],
            mode="lines", name="DIF",
            line=dict(width=1, color="white"),
        ), row=current_row, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["dea"],
            mode="lines", name="DEA",
            line=dict(width=1, color="yellow"),
        ), row=current_row, col=1)

    # 布局
    fig.update_layout(
        height=600 + (rows - 1) * 100,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
        margin=dict(l=50, r=20, t=50, b=20),
    )

    # 隐藏非交易日的空隙
    fig.update_xaxes(type="category", nticks=20)

    return fig


def show():
    """数据浏览页面"""
    st.title("📈 数据浏览")

    # 加载股票列表
    stock_df = get_stock_list()
    if stock_df.empty:
        st.warning("数据库中暂无股票数据，请先运行初始化脚本：")
        st.code("python scripts/init_data.py", language="bash")
        return

    # 上方筛选区
    with st.expander("🔍 筛选条件", expanded=True):
        col_search, col_date, col_ind = st.columns([2, 3, 2])

        with col_search:
            st.markdown("**股票搜索**")
            search = st.text_input("输入代码或名称", placeholder="如: 000001 或 平安银行",
                                   label_visibility="collapsed")

            if search:
                mask = (stock_df["ts_code"].str.contains(search, case=False) |
                        stock_df["name"].str.contains(search, case=False))
                filtered = stock_df[mask]
            else:
                # 默认只显示有数据的股票（自选股优先）
                codes_with_data = get_stocks_with_data(min_days=1)
                filtered = stock_df[stock_df["ts_code"].isin(codes_with_data)].head(50)

            if filtered.empty:
                st.warning("未找到匹配的股票")
                # 不 return，让页面保持显示，用户可以继续搜索
                st.stop()

            options = filtered.apply(
                lambda r: f"{r['ts_code']} {r['name']}", axis=1
            ).tolist()

            selected = st.selectbox("选择股票", options, index=0,
                                    label_visibility="collapsed")
            ts_code = selected.split(" ")[0]
            stock_name = selected.split(" ", 1)[1] if " " in selected else ""

        with col_date:
            start_date, end_date = chinese_date_input(
                "日期范围",
                default_start=date(2024, 1, 1),
                default_end=date.today(),
                key="dv",
            )

        with col_ind:
            st.markdown("**📊 指标叠加**")
            show_volume = st.checkbox("成交量", value=True)
            show_ma = st.checkbox("均线 (MA5/10/20/60)", value=True)
            show_macd = st.checkbox("MACD", value=False)
            show_boll = st.checkbox("布林带", value=False)

    # 获取数据
    df = get_daily(ts_code, start_date, end_date)

    if df.empty:
        st.warning(f"{ts_code} {stock_name} 在所选日期范围内没有数据")
        return

    df = clean_daily(df)
    if df.empty:
        st.warning("数据清洗后为空")
        return

    # 计算指标
    indicator_list = []
    if show_ma:
        indicator_list.append("ma")
    if show_macd:
        indicator_list.append("macd")
    if show_boll:
        indicator_list.append("boll")
    indicator_list.append("vol_ma")
    df = apply_indicators(df, indicator_list)

    # 顶部指标卡片
    latest = df.iloc[-1]
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("最新价", f"¥{latest['close']:.2f}")
    with col2:
        chg = latest.get("pct_chg", 0)
        st.metric("涨跌幅", f"{chg:.2f}%",
                   delta=f"{chg:.2f}%", delta_color="normal")
    with col3:
        st.metric("成交量", f"{latest['volume'] / 10000:.0f}万手")
    with col4:
        st.metric("成交额", f"{latest['amount'] / 100000:.0f}万元" if latest['amount'] else "N/A")
    with col5:
        st.metric("数据条数", f"{len(df)}")

    # K线图
    fig = create_candlestick_chart(
        df, ts_code, stock_name,
        show_volume=show_volume,
        show_ma=show_ma,
        show_macd=show_macd,
        show_boll=show_boll,
    )
    st.plotly_chart(fig, width='stretch')

    # 最近数据表格
    with st.expander("📋 最近30条数据"):
        display_cols = ["trade_date", "open", "high", "low", "close",
                        "volume", "pct_chg"]
        display_df = df[display_cols].tail(30).copy()
        display_df = display_df.rename(columns={
            "trade_date": "日期", "open": "开盘", "high": "最高",
            "low": "最低", "close": "收盘", "volume": "成交量",
            "pct_chg": "涨跌幅%",
        })
        chinese_dataframe(display_df)

