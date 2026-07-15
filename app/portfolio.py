"""持仓管理页面 - 自选股 + 模拟持仓 + 信号自动跟单"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, datetime, timedelta

from data.storage import (get_watchlist, add_to_watchlist, remove_from_watchlist,
                          get_stock_list, get_daily, get_signals, get_latest_date,
                          save_daily, get_positions, add_position, remove_position)
from data.fetcher import fetch_daily
from data.cleaner import clean_daily
from core.config import DATA_START_DATE
from app.st_utils import chinese_dataframe, chinese_date_picker
from data.indicators import apply_indicators
from strategies import STRATEGY_REGISTRY
from engine.scanner import scan_signals


def show():
    st.title("💼 持仓管理")

    tab1, tab2, tab3 = st.tabs(["自选股", "模拟持仓", "信号自动跟单"])

    with tab1:
        _show_watchlist()
    with tab2:
        _show_portfolio()
    with tab3:
        _show_auto_trade()


def _show_watchlist():
    """自选股管理"""
    st.subheader("自选股列表")

    stock_df = get_stock_list()

    # 添加自选股
    col1, col2 = st.columns([3, 1])
    with col1:
        search = st.text_input("搜索股票", placeholder="输入代码或名称搜索", key="wl_search")
    with col2:
        st.markdown("")  # 占位对齐
        note = st.text_input("备注", placeholder="如: 看好新能源", key="wl_note")

    if search and stock_df is not None and not stock_df.empty:
        mask = (stock_df["ts_code"].str.contains(search, case=False) |
                stock_df["name"].str.contains(search, case=False))
        filtered = stock_df[mask].head(10)
        if not filtered.empty:
            options = filtered.apply(
                lambda r: f"{r['ts_code']} {r['name']}", axis=1
            ).tolist()
            col_a, col_b = st.columns([3, 1])
            with col_a:
                selected = st.selectbox("选择要添加的股票", options, key="wl_select",
                                        label_visibility="collapsed")
                download_data = st.checkbox("同时下载历史数据", value=True, key="wl_download",
                                            help="勾选后将自动下载近5年的历史数据并扫描信号")
            with col_b:
                if st.button("✅ 确认添加", key="wl_add"):
                    ts_code = selected.split(" ")[0]
                    add_to_watchlist(ts_code, st.session_state.wl_note)

                    # 自动下载该股票的历史数据（如果用户勾选）
                    if download_data:
                        latest = get_latest_date(ts_code)
                        end = datetime.now().strftime("%Y%m%d")
                        data_ready = False
                        if not latest or latest < end:
                            with st.spinner(f"⏳ 正在下载 {selected} 的历史数据..."):
                                df = fetch_daily(ts_code, DATA_START_DATE, end)
                                if not df.empty:
                                    df = clean_daily(df)
                                    save_daily(df)
                                    data_ready = True
                                    st.toast(f"✅ {selected} 数据已就绪 ({len(df)} 条)", icon="📊")
                                else:
                                    st.warning(f"{selected} 暂无可用数据，稍后可通过更新获取")

                        # 数据就绪后，自动扫描一次信号
                        if data_ready:
                            with st.spinner(f"🔍 正在扫描 {selected} 的信号..."):
                                signals = scan_signals(
                                    universe=[ts_code],
                                    strategy_names=list(STRATEGY_REGISTRY.keys()),
                                    end_date=end,
                                    save=True,
                                )
                                if signals:
                                    st.toast(f"📡 {selected} 发现 {len(signals)} 条信号", icon="📡")
                                else:
                                    st.toast(f"📡 {selected} 暂无信号", icon="✅")

                    st.success(f"已添加 {selected}")
                    st.rerun()
        else:
            st.info("未找到匹配的股票")
    elif search:
        st.info("未找到匹配的股票")

    # 显示自选股列表
    watchlist = get_watchlist()
    if watchlist.empty:
        st.info("暂无自选股，请添加")
        return

    # 分组筛选
    from data.storage import get_watchlist_groups, update_watchlist_group
    groups = get_watchlist_groups()
    group_filter = st.selectbox("按分组筛选", ["全部", *groups], key="wl_group_filter")
    if group_filter != "全部":
        watchlist = watchlist[watchlist["group_name"] == group_filter]

    # 获取每只自选股的最新价格
    rows = []
    for _, wl_row in watchlist.iterrows():
        ts_code = wl_row["ts_code"]
        df = get_daily(ts_code)
        if df.empty:
            rows.append({
                "代码": ts_code, "名称": ts_code,
                "最新价": "-", "涨跌幅": "-",
                "分组": wl_row.get("group_name", ""),
                "备注": wl_row.get("note", ""),
            })
            continue

        latest = df.iloc[-1]
        name = ts_code
        if not stock_df.empty:
            match = stock_df[stock_df["ts_code"] == ts_code]
            if not match.empty:
                name = match.iloc[0]["name"]

        rows.append({
            "代码": ts_code,
            "名称": name,
            "最新价": f"¥{latest['close']:.2f}",
            "涨跌幅": f"{latest.get('pct_chg', 0):.2f}%",
            "分组": wl_row.get("group_name", ""),
            "备注": wl_row.get("note", ""),
        })

    df = pd.DataFrame(rows)
    chinese_dataframe(df)

    # 删除自选股
    with st.expander("移除自选股"):
        remove_code = st.selectbox(
            "选择要移除的股票",
            watchlist["ts_code"].tolist(),
        )
        if st.button("移除", type="secondary"):
            remove_from_watchlist(remove_code)
            st.success(f"已移除 {remove_code}")
            st.rerun()

    # 分组管理
    with st.expander("分组管理"):
        group_code = st.selectbox("选择股票", watchlist["ts_code"].tolist(), key="group_code")
        group_name = st.text_input("分组名称", placeholder="如: 长线池、短线池", key="group_name_input",
                                    value=watchlist[watchlist["ts_code"] == group_code]["group_name"].iloc[0]
                                    if group_code in watchlist["ts_code"].values else "")
        if st.button("保存分组", key="save_group"):
            update_watchlist_group(group_code, group_name)
            st.success(f"已设置 {group_code} 分组为 {group_name}")
            st.rerun()

    # 自选股K线快览
    st.subheader("K线快览")
    if not watchlist.empty:
        selected_code = st.selectbox("查看K线", watchlist["ts_code"].tolist())
        if selected_code:
            df = get_daily(selected_code)
            if not df.empty:
                df = apply_indicators(df, ["ma"])
                fig = go.Figure()
                fig.add_trace(go.Candlestick(
                    x=df["trade_date"], open=df["open"], high=df["high"],
                    low=df["low"], close=df["close"],
                    increasing_line_color="red", decreasing_line_color="green",
                    increasing_fillcolor="red", decreasing_fillcolor="green",
                ))
                if "ma20" in df.columns:
                    fig.add_trace(go.Scatter(
                        x=df["trade_date"], y=df["ma20"],
                        mode="lines", name="MA20",
                        line=dict(color="#45B7D1", width=1),
                    ))
                fig.update_layout(
                    height=350, template="plotly_dark",
                    xaxis_rangeslider_visible=False,
                    xaxis=dict(type="category", nticks=15),
                    margin=dict(l=30, r=10, t=30, b=30),
                )
                st.plotly_chart(fig, width='stretch')


def _show_portfolio():
    """模拟持仓管理（持久化到 SQLite）"""
    st.subheader("模拟持仓")
    st.caption("记录你的实际持仓，跟踪盈亏（数据持久化到数据库）")

    stock_df = get_stock_list()

    # 添加持仓
    with st.form("add_position", clear_on_submit=True):
        st.markdown("**添加持仓**")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            code_input = st.text_input("股票代码", placeholder="000001.SZ")
        with col2:
            buy_price = st.number_input("买入价", min_value=0.01, step=0.01)
        with col3:
            shares = st.number_input("数量(股)", min_value=100, step=100)
        with col4:
            buy_date = chinese_date_picker("买入日期", key="buy_date")

        if st.form_submit_button("添加"):
            if code_input and buy_price > 0 and shares > 0:
                add_position(code_input.strip(), buy_price, shares, buy_date)
                st.rerun()

    positions = get_positions()
    if not positions:
        st.info("暂无持仓记录，请添加")
        return

    # 显示持仓盈亏
    rows = []
    total_cost = 0
    total_value = 0

    for pos in positions:
        ts_code = pos["ts_code"]
        df = get_daily(ts_code)
        current_price = df.iloc[-1]["close"] if not df.empty else pos["buy_price"]

        cost = pos["buy_price"] * pos["shares"]
        value = current_price * pos["shares"]
        pnl = value - cost
        pnl_pct = (current_price / pos["buy_price"] - 1) * 100

        name = ts_code
        if not stock_df.empty:
            match = stock_df[stock_df["ts_code"] == ts_code]
            if not match.empty:
                name = match.iloc[0]["name"]

        total_cost += cost
        total_value += value

        rows.append({
            "代码": ts_code,
            "名称": name,
            "买入价": f"¥{pos['buy_price']:.2f}",
            "现价": f"¥{current_price:.2f}",
            "数量": pos["shares"],
            "成本": f"¥{cost:.0f}",
            "市值": f"¥{value:.0f}",
            "盈亏": f"¥{pnl:.0f}",
            "盈亏%": f"{pnl_pct:.2f}%",
        })

    df = pd.DataFrame(rows)
    chinese_dataframe(df)

    # 汇总
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总成本", f"¥{total_cost:,.0f}")
    c2.metric("总市值", f"¥{total_value:,.0f}")
    c3.metric("总盈亏", f"¥{total_pnl:,.0f}")
    c4.metric("收益率", f"{total_pnl_pct:.2f}%")

    # 清空持仓
    if st.button("清空所有持仓"):
        st.session_state.positions = []
        st.rerun()


def _show_auto_trade():
    """信号自动跟单 - 根据信号中心的最新信号自动更新模拟持仓"""
    st.subheader("📡 信号自动跟单")
    st.caption("根据信号中心的最新买入/卖出信号，自动生成模拟交易建议")

    col1, col2 = st.columns(2)
    with col1:
        auto_days = st.selectbox("取最近N天的信号", [1, 3, 5, 7], index=0)
    with col2:
        min_score = st.slider("最低信号评分", 0.0, 1.0, 0.6, 0.05)

    if st.button("🔄 读取最新信号并生成跟单建议", type="primary"):
        # 获取最近的信号
        trade_date = (datetime.now() - timedelta(days=auto_days)).strftime("%Y%m%d")
        signals_df = get_signals(limit=200)

        if signals_df.empty:
            st.info("暂无信号数据，请先在信号中心运行扫描")
            return

        # 过滤
        if trade_date:
            signals_df = signals_df[signals_df["trade_date"] >= trade_date]
        signals_df = signals_df[signals_df["score"] >= min_score]

        if signals_df.empty:
            st.info(f"最近{auto_days}天无评分≥{min_score}的信号")
            return

        stock_df = get_stock_list()
        name_map = {}
        if not stock_df.empty:
            name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

        buy_signals = signals_df[signals_df["direction"] == "BUY"]
        sell_signals = signals_df[signals_df["direction"] == "SELL"]

        existing_positions = get_positions()
        existing_codes = [p["ts_code"] for p in existing_positions]

        # 自动跟单建议
        st.subheader(f"🟢 买入建议 ({len(buy_signals)})")
        if not buy_signals.empty:
            buy_rows = []
            for _, sig in buy_signals.iterrows():
                has_pos = "已有" if sig["ts_code"] in existing_codes else "新开"

                buy_rows.append({
                    "股票": f"{sig['ts_code']} {name_map.get(sig['ts_code'], '')}",
                    "信号价": f"¥{sig['price_ref']:.2f}" if sig["price_ref"] else "-",
                    "评分": sig["score"],
                    "策略": sig["strategy"],
                    "状态": has_pos,
                    "原因": sig.get("reason", ""),
                })

            df_buy = pd.DataFrame(buy_rows)
            chinese_dataframe(df_buy)

            # 一键跟单按钮
            if st.button("📥 一键跟入（将买入信号加入持仓）"):
                added = 0
                for _, sig in buy_signals.iterrows():
                    if sig["ts_code"] not in existing_codes and sig["price_ref"] > 0:
                        budget_per_stock = 50000
                        shares = int(budget_per_stock / sig["price_ref"]) // 100 * 100
                        if shares >= 100:
                            add_position(sig["ts_code"], sig["price_ref"], shares, sig["trade_date"])
                            added += 1
                st.success(f"已跟入 {added} 只股票到模拟持仓")
                st.rerun()
        else:
            st.info("无买入信号")

        st.subheader(f"🔴 卖出建议 ({len(sell_signals)})")
        if not sell_signals.empty:
            sell_rows = []
            for _, sig in sell_signals.iterrows():
                pos = next((p for p in existing_positions if p["ts_code"] == sig["ts_code"]), None)
                sell_rows.append({
                    "股票": f"{sig['ts_code']} {name_map.get(sig['ts_code'], '')}",
                    "信号价": f"¥{sig['price_ref']:.2f}" if sig["price_ref"] else "-",
                    "评分": sig["score"],
                    "策略": sig["strategy"],
                    "持仓": f"{pos['shares']}股" if pos else "无",
                    "原因": sig.get("reason", ""),
                })

            df_sell = pd.DataFrame(sell_rows)
            chinese_dataframe(df_sell)

            if st.button("📤 一键跟出（将卖出信号从持仓移除）"):
                removed = 0
                for sig_ts_code in sell_signals["ts_code"].tolist():
                    pos = next((p for p in get_positions() if p["ts_code"] == sig_ts_code), None)
                    if pos:
                        remove_position(pos["_id"])
                        removed += 1
                st.success(f"已移除 {removed} 只持仓")
                st.rerun()
        else:
            st.info("无卖出信号")

    # 显示当前持仓的买卖点标注在K线上
    st.subheader("📈 持仓K线（含买卖点标注）")
    current_positions = get_positions()
    if current_positions:
        pos_code = st.selectbox(
            "选择持仓股票查看K线",
            [p["ts_code"] for p in current_positions],
        )
        if pos_code:
            df = get_daily(pos_code)
            if not df.empty:
                df = apply_indicators(df, ["ma"])
                fig = go.Figure()
                fig.add_trace(go.Candlestick(
                    x=df["trade_date"], open=df["open"], high=df["high"],
                    low=df["low"], close=df["close"],
                    increasing_line_color="red", decreasing_line_color="green",
                    increasing_fillcolor="red", decreasing_fillcolor="green",
                    name="K线",
                ))
                if "ma20" in df.columns:
                    fig.add_trace(go.Scatter(
                        x=df["trade_date"], y=df["ma20"],
                        mode="lines", name="MA20",
                        line=dict(color="#45B7D1", width=1),
                    ))

                # 标注买卖点
                for p in current_positions:
                    if p["ts_code"] == pos_code:
                        buy_date = p["buy_date"]
                        if buy_date in df["trade_date"].values:
                            buy_row = df[df["trade_date"] == buy_date].iloc[0]
                            fig.add_annotation(
                                x=buy_date,
                                y=buy_row["low"],
                                text="🟢 买入",
                                showarrow=True,
                                arrowhead=2,
                                arrowsize=1.5,
                                arrowcolor="green",
                                font=dict(color="green", size=12),
                            )

                fig.update_layout(
                    height=400,
                    template="plotly_dark",
                    xaxis_rangeslider_visible=False,
                    xaxis=dict(type="category", nticks=15),
                    margin=dict(l=30, r=10, t=30, b=30),
                )
                st.plotly_chart(fig, width='stretch')
    else:
        st.info("暂无持仓，可通过信号跟单或手动添加")

