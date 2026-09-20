"""数据浏览页面 - K线图 + 成交量 + 指标"""
import app  # noqa: F401 (ensure project root in path)
from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from data.storage import get_stock_list, get_daily, get_daily_count, get_stocks_with_data, get_instrument_list
from app.st_utils import chinese_dataframe, chinese_date_input, cached_get_stocks_with_data, cached_instrument_list
from data.indicators import apply_indicators
from data.cleaner import clean_daily


def create_candlestick_chart(df: pd.DataFrame, ts_code: str, name: str,
                             show_volume: bool = True,
                             show_ma: bool = True,
                             show_macd: bool = False,
                             show_boll: bool = False,
                             buy_dates: list = None,
                             sell_dates: list = None,
                             height: int = None) -> go.Figure:
    """创建K线图（Plotly）— 同花顺风格

    特点（仿同花顺）：
      - 红涨绿跌（A股配色）
      - 主图：K线 + MA5/10/20/60 均线（白/黄/品红/亮绿）
      - 副图：成交量（红绿柱+均量线）+ MACD（柱+DIF/DEA）
      - 十字光标联动 + 昨收/最新价虚线标注
      - 深色金融背景，隐藏非交易日空隙
      - 可选买卖点标注（红买绿卖箭头）

    参数:
        buy_dates:  买入日期列表 ['YYYYMMDD', ...]，标注红色"买"箭头
        sell_dates: 卖出日期列表 ['YYYYMMDD', ...]，标注绿色"卖"箭头
        height:     图表高度（默认自动计算）
    """
    if df is None or df.empty:
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(13,21,38,0.6)",
            title=f"{name or ts_code} - 暂无行情数据",
        )
        return fig

    # 同花顺均线配色：MA5白 / MA10黄 / MA20品红 / MA60亮绿
    MA_COLORS = {
        "ma5": "#FFFFFF",
        "ma10": "#FFD700",
        "ma20": "#FF69B4",
        "ma60": "#00FF7F",
    }
    # A股涨跌配色（与全局主题一致）
    UP = "#ef5350"
    DOWN = "#26a69a"

    # 子图标题（名称与代码重复时只显示代码）
    if name and name != ts_code:
        title_text = f"{name} ({ts_code})"
    else:
        title_text = ts_code

    # 确定子图数量
    rows = 1
    row_heights = [0.62]
    subplot_titles = [title_text]

    if show_volume:
        rows += 1
        row_heights.append(0.16)
        subplot_titles.append("成交量")

    if show_macd:
        rows += 1
        row_heights.append(0.14)
        subplot_titles.append("MACD")

    # 归一化高度
    total = sum(row_heights)
    row_heights = [h / total for h in row_heights]

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )

    # ---------- 主图: K线 ----------
    fig.add_trace(go.Candlestick(
        x=df["trade_date"],
        open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing_line_color=UP,
        decreasing_line_color=DOWN,
        increasing_fillcolor=UP,
        decreasing_fillcolor=DOWN,
        name="K线",
        whiskerwidth=0.4,
    ), row=1, col=1)

    # ---------- 主图: 均线（同花顺配色） ----------
    if show_ma:
        for col, color in MA_COLORS.items():
            if col in df.columns:
                fig.add_trace(go.Scatter(
                    x=df["trade_date"], y=df[col],
                    mode="lines", name=col.upper(),
                    line=dict(width=1.2, color=color),
                ), row=1, col=1)

    # ---------- 主图: 布林带 ----------
    if show_boll and "boll_upper" in df.columns:
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_upper"],
            mode="lines", name="BOLL上轨",
            line=dict(width=1, color="rgba(150,160,180,0.7)", dash="dash"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_mid"],
            mode="lines", name="BOLL中轨",
            line=dict(width=1, color="rgba(150,160,180,0.9)"),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["boll_lower"],
            mode="lines", name="BOLL下轨",
            line=dict(width=1, color="rgba(150,160,180,0.7)", dash="dash"),
            fill="tonexty", fillcolor="rgba(150,160,180,0.08)",
        ), row=1, col=1)

    # ---------- 主图: 昨收虚线 ----------
    if len(df) > 1:
        prev_close = float(df["close"].iloc[-2])
        fig.add_hline(
            y=prev_close, row=1, col=1,
            line=dict(color="rgba(255,255,255,0.30)", width=1, dash="dash"),
            annotation_text="昨收", annotation_position="top left",
            annotation_font=dict(size=10, color="rgba(255,255,255,0.45)"),
        )

    # ---------- 主图: 最新价虚线（金色标注） ----------
    last_close = float(df["close"].iloc[-1])
    fig.add_hline(
        y=last_close, row=1, col=1,
        line=dict(color="#FFD700", width=1.2, dash="dot"),
        annotation_text=f"最新 {last_close:.2f}",
        annotation_position="top right",
        annotation_font=dict(size=11, color="#FFD700"),
    )

    current_row = 1

    # ---------- 副图: 成交量（红涨绿跌柱 + 均量线） ----------
    if show_volume:
        current_row += 1
        colors = [UP if c >= o else DOWN
                  for c, o in zip(df["close"], df["open"])]
        fig.add_trace(go.Bar(
            x=df["trade_date"], y=df["volume"],
            marker_color=colors, name="成交量",
            showlegend=False,
        ), row=current_row, col=1)

        # 成交量均线（同花顺: 黄/品红）
        if "vol_ma5" in df.columns:
            fig.add_trace(go.Scatter(
                x=df["trade_date"], y=df["vol_ma5"],
                mode="lines", name="VOL_MA5",
                line=dict(width=1, color="#FFD700"),
            ), row=current_row, col=1)
        if "vol_ma10" in df.columns:
            fig.add_trace(go.Scatter(
                x=df["trade_date"], y=df["vol_ma10"],
                mode="lines", name="VOL_MA10",
                line=dict(width=1, color="#FF69B4"),
            ), row=current_row, col=1)

    # ---------- 副图: MACD ----------
    if show_macd and "dif" in df.columns:
        current_row += 1
        macd_colors = [UP if v >= 0 else DOWN for v in df["macd_hist"]]
        fig.add_trace(go.Bar(
            x=df["trade_date"], y=df["macd_hist"],
            marker_color=macd_colors, name="MACD柱",
            showlegend=False,
        ), row=current_row, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["dif"],
            mode="lines", name="DIF",
            line=dict(width=1.2, color="#FFFFFF"),
        ), row=current_row, col=1)
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["dea"],
            mode="lines", name="DEA",
            line=dict(width=1.2, color="#FFD700"),
        ), row=current_row, col=1)

    # ---------- 布局: 同花顺深色风格 ----------
    fig.update_layout(
        height=height or (600 + (rows - 1) * 120),
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(13,21,38,0.6)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=11)),
        margin=dict(l=50, r=20, t=50, b=20),
    )

    # 十字光标（同花顺风格：悬停显示十字线 + 统一数据条）
    fig.update_xaxes(
        showspikes=True, spikemode="across",
        spikecolor="rgba(150,160,180,0.4)", spikethickness=1,
        spikesnap="cursor",
    )
    fig.update_yaxes(
        showspikes=True, spikemode="across",
        spikecolor="rgba(150,160,180,0.4)", spikethickness=1,
        spikesnap="cursor",
    )

    # 隐藏非交易日的空隙
    fig.update_xaxes(type="category", nticks=20)

    # ---------- 买卖点标注（同花顺风格：红买绿卖箭头） ----------
    if buy_dates:
        for bd in buy_dates:
            if bd in df["trade_date"].values:
                row = df[df["trade_date"] == bd].iloc[0]
                fig.add_annotation(
                    x=bd, y=row["low"],
                    text="买", showarrow=True,
                    arrowhead=2, arrowsize=1.4,
                    arrowcolor=UP, ax=0, ay=18,
                    font=dict(color=UP, size=13),
                    bordercolor=UP, borderpad=3,
                    bgcolor="rgba(239,83,80,0.12)",
                )
    if sell_dates:
        for sd in sell_dates:
            if sd in df["trade_date"].values:
                row = df[df["trade_date"] == sd].iloc[0]
                fig.add_annotation(
                    x=sd, y=row["high"],
                    text="卖", showarrow=True,
                    arrowhead=2, arrowsize=1.4,
                    arrowcolor=DOWN, ax=0, ay=-18,
                    font=dict(color=DOWN, size=13),
                    bordercolor=DOWN, borderpad=3,
                    bgcolor="rgba(38,166,154,0.12)",
                )

    return fig


def show():
    """数据浏览页面"""
    st.title("📈 数据浏览")

    # 加载股票 + ETF 合并列表
    stock_df = cached_instrument_list()
    if stock_df.empty:
        st.warning("数据库中暂无标的（股票/ETF）数据，请先运行初始化脚本：")
        st.code("python scripts/init_data.py", language="bash")
        return

    # 上方筛选区
    with st.expander("🔍 筛选条件", expanded=True):
        col_search, col_date, col_ind = st.columns([2, 3, 2])

        with col_search:
            st.markdown("**标的搜索**")
            search = st.text_input("输入代码或名称", placeholder="如: 000001 / 510300 或 平安银行 / 沪深300",
                                   label_visibility="collapsed")

            if search:
                mask = (stock_df["ts_code"].str.contains(search, case=False, regex=False) |
                        stock_df["name"].str.contains(search, case=False, regex=False))
                filtered = stock_df[mask]
            else:
                # 默认只显示有数据的标的（自选股优先）
                codes_with_data = cached_get_stocks_with_data(min_days=1)
                filtered = stock_df[stock_df["ts_code"].isin(codes_with_data)].head(50)

            if filtered.empty:
                st.warning("未找到匹配的标的")
                # 不 return，让页面保持显示，用户可以继续搜索
                st.stop()

            options = filtered.apply(
                lambda r: f"[{r['type']}] {r['ts_code']} {r['name']}", axis=1
            ).tolist()

            selected = st.selectbox("选择标的", options, index=0,
                                    label_visibility="collapsed")
            parts = selected.split(" ")
            ts_code = parts[1] if len(parts) >= 2 else parts[0]
            # 名称可能含空格，取 type/code 之后的剩余部分
            stock_name = selected.split(" ", 2)[2] if len(selected.split(" ", 2)) >= 3 else ""

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

    # 顶部指标卡片（同花顺风格：最新价/涨跌幅/今开/昨收/最高/最低）
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    chg = float(latest.get("pct_chg", 0) or 0)
    chg_color = "inverse"  # 交给 delta_color 按正负显示 (红涨绿跌)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("最新价", f"¥{latest['close']:.2f}",
                  delta=f"{chg:+.2f}%", delta_color="inverse")
    with col2:
        st.metric("今开", f"¥{latest['open']:.2f}",
                  delta=f"{latest['open'] - prev['close']:+.2f}",
                  delta_color="inverse")
    with col3:
        st.metric("昨收", f"¥{prev['close']:.2f}")
    with col4:
        st.metric("数据条数", f"{len(df)}")

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        st.metric("最高", f"¥{latest['high']:.2f}")
    with col6:
        st.metric("最低", f"¥{latest['low']:.2f}")
    with col7:
        vol_shares = float(latest.get("volume", 0) or 0)
        vol_str = f"{vol_shares / 1e6:.2f}万手" if vol_shares >= 1e6 else f"{vol_shares / 100:.0f}手"
        st.metric("成交量", vol_str)
    with col8:
        st.metric("成交额", f"{latest['amount'] / 1e8:.2f}亿" if latest['amount'] else "N/A")

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
        display_df["volume"] = (display_df["volume"] / 100).round(0).astype(int)
        display_df = display_df.rename(columns={
            "trade_date": "日期", "open": "开盘", "high": "最高",
            "low": "最低", "close": "收盘", "volume": "成交量(手)",
            "pct_chg": "涨跌幅%",
        })
        chinese_dataframe(display_df)

