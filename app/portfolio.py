"""持仓管理页面 - 自选股 + 模拟持仓 + 信号自动跟单"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta

from data.storage import (get_watchlist, add_to_watchlist, remove_from_watchlist,
                          get_stock_list, get_daily, get_signals, get_latest_date,
                          save_daily, get_positions, add_position, remove_position,
                          clear_positions, get_instrument_list)
from data.fetcher import fetch_daily, fetch_instrument_daily
from data.cleaner import clean_daily
from core.config import DATA_START_DATE
from app.st_utils import chinese_dataframe, chinese_date_picker, cached_instrument_list, format_strategy_cn, format_stock_cn
from app.data_viewer import create_candlestick_chart
from data.indicators import apply_indicators
from strategies import STRATEGY_REGISTRY
from engine.scanner import scan_signals
from services.validators import normalize_stock_code


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

    stock_df = cached_instrument_list()  # 股票 + ETF 合并列表（已缓存）

    # 添加自选股
    col1, col2 = st.columns([3, 1])
    with col1:
        search = st.text_input("搜索股票/ETF", placeholder="输入代码或名称搜索", key="wl_search")
    with col2:
        st.markdown("")  # 占位对齐
        note = st.text_input("备注", placeholder="如: 看好新能源", key="wl_note")

    if search and stock_df is not None and not stock_df.empty:
        mask = (stock_df["ts_code"].str.contains(search, case=False, regex=False) |
                stock_df["name"].str.contains(search, case=False, regex=False))
        filtered = stock_df[mask].head(10)
        if not filtered.empty:
            options = filtered.apply(
                lambda r: f"[{r['type']}] {r['ts_code']} {r['name']}", axis=1
            ).tolist()
            col_a, col_b = st.columns([3, 1])
            with col_a:
                selected = st.selectbox("选择要添加的标的", options, key="wl_select",
                                        label_visibility="collapsed")
                download_data = st.checkbox("同时下载历史数据", value=True, key="wl_download",
                                            help="勾选后将自动下载近5年的历史数据并扫描信号")
            with col_b:
                if st.button("✅ 确认添加", key="wl_add"):
                    parts = selected.split(" ")
                    ts_code = parts[1] if len(parts) >= 2 else parts[0]
                    add_to_watchlist(ts_code, st.session_state.wl_note)

                    # 自动下载该标的的历史数据（如果用户勾选）
                    if download_data:
                        latest = get_latest_date(ts_code)
                        end = datetime.now().strftime("%Y%m%d")
                        data_ready = False
                        if not latest or latest < end:
                            with st.spinner(f"⏳ 正在下载 {selected} 的历史数据..."):
                                df = fetch_instrument_daily(ts_code, DATA_START_DATE, end)
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

    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}

    # 获取每只自选股的最新价格
    rows = []
    for _, wl_row in watchlist.iterrows():
        ts_code = wl_row["ts_code"]
        name = name_map.get(ts_code, ts_code)
        df = get_daily(ts_code)
        if df.empty:
            rows.append({
                "股票": format_stock_cn(ts_code, name_map),
                "最新价": "-",
                "涨跌幅": "-",
                "分组": wl_row.get("group_name", ""),
                "备注": wl_row.get("note", ""),
            })
            continue

        latest = df.iloc[-1]

        rows.append({
            "股票": format_stock_cn(ts_code, name_map),
            "最新价": f"¥{latest['close']:.2f}",
            "涨跌幅": f"{latest.get('pct_chg', 0):.2f}%",
            "分组": wl_row.get("group_name", ""),
            "备注": wl_row.get("note", ""),
        })

    df = pd.DataFrame(rows)
    chinese_dataframe(df)

    # 导出自选股 CSV
    csv_data = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 导出自选股 CSV",
        data=csv_data,
        file_name=f"自选股_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    # 构造标的代码与中文名称映射
    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if stock_df is not None and not stock_df.empty else {}
    format_stock = lambda code: f"{code} {name_map.get(code, '')}".strip()

    # 删除自选股（通过表单封装，彻底消除勾选即触发刷新和跳顶问题）
    with st.expander("移除自选股", expanded=False):
        with st.form("remove_watchlist_form", clear_on_submit=True):
            remove_codes = st.multiselect(
                "选择要移除的股票（支持多选，勾选不会刷新页面）",
                watchlist["ts_code"].tolist(),
                format_func=format_stock,
                key="remove_multiselect",
            )
            col_rm1, col_rm2 = st.columns([1, 2])
            with col_rm1:
                confirm_del = st.form_submit_button("🗑️ 确认批量移除", type="primary", width="stretch")
            with col_rm2:
                st.caption("💡 提示：支持同时勾选多只股票/ETF，点击按钮后一次性批量移除，操作过程中绝不跳动页面")

            if confirm_del:
                if not remove_codes:
                    st.warning("请先勾选需要移除的股票")
                else:
                    for code in remove_codes:
                        remove_from_watchlist(code)
                    st.toast(f"✅ 已成功移除 {len(remove_codes)} 只自选股", icon="🗑️")
                    st.rerun()

    # 分组管理（通过表单封装，彻底消除勾选即触发刷新和跳顶问题）
    with st.expander("分组管理", expanded=False):
        with st.form("group_watchlist_form", clear_on_submit=False):
            group_codes = st.multiselect(
                "选择股票（支持多选批量分组，勾选不会刷新页面）",
                watchlist["ts_code"].tolist(),
                format_func=format_stock,
                key="group_multiselect",
            )

            col_g1, col_g2 = st.columns([1, 2])
            with col_g1:
                existing_groups = ["-- 自定义输入 --"] + [g for g in groups if g]
                preset_group = st.selectbox("从已有分组选取", existing_groups, key="preset_group_pick")
            with col_g2:
                default_val = "" if preset_group == "-- 自定义输入 --" else preset_group
                group_name = st.text_input(
                    "目标分组名称",
                    value=default_val,
                    placeholder="如: 核心资产、短线博弈、新能源池（留空即清空分组）",
                    key="group_name_input",
                )

            submit_group = st.form_submit_button("💾 保存分组设置", type="primary", width="content")
            if submit_group:
                if not group_codes:
                    st.warning("请至少勾选一只股票")
                else:
                    target_name = group_name.strip()
                    for code in group_codes:
                        update_watchlist_group(code, target_name)
                    st.toast(f"✅ 已将选中的 {len(group_codes)} 只标的分组设置为: 「{target_name or '默认'}」", icon="💾")
                    st.rerun()


    # 自选股K线快览
    st.subheader("K线快览")
    if not watchlist.empty:
        selected_code = st.selectbox(
            "查看K线",
            watchlist["ts_code"].tolist(),
            format_func=format_stock,
        )
        if selected_code:
            stock_display_name = name_map.get(selected_code, selected_code)
            df = get_daily(selected_code, limit=250)
            if not df.empty:
                df = apply_indicators(df, ["ma", "vol_ma"])
                fig = create_candlestick_chart(
                    df, selected_code, stock_display_name,
                    show_volume=True, show_macd=False, show_boll=False,
                    height=400,
                )
                st.plotly_chart(fig, width='stretch')



def _show_portfolio():
    """模拟持仓管理（持久化到 SQLite）"""
    st.subheader("模拟持仓")
    st.caption("记录你的实际持仓，跟踪盈亏（数据持久化到数据库）")

    stock_df = cached_instrument_list()  # 股票 + ETF 合并列表（用于持仓名称显示）
    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}

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
                code_str = normalize_stock_code(code_input)
                add_position(code_str, buy_price, shares, buy_date)
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
        df = get_daily(ts_code, limit=1)
        current_price = df.iloc[-1]["close"] if not df.empty else pos["buy_price"]

        cost = pos["buy_price"] * pos["shares"]
        value = current_price * pos["shares"]
        pnl = value - cost
        pnl_pct = (current_price / pos["buy_price"] - 1) * 100

        name = name_map.get(ts_code, ts_code)

        total_cost += cost
        total_value += value

        rows.append({
            "股票": format_stock_cn(ts_code, name_map),
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

    col_del1, col_del2 = st.columns([1, 1])
    with col_del1:
        if st.button("🗑️ 清空所有持仓", type="secondary"):
            clear_positions()
            st.session_state.positions = []
            st.toast("已清空所有模拟持仓", icon="🗑️")
            st.rerun()
    with col_del2:
        with st.popover("❌ 移除单只持仓"):
            del_pos_choice = st.selectbox(
                "选择要移除的持仓",
                positions,
                format_func=lambda p: f"{p['ts_code']} {name_map.get(p['ts_code'], '')} ({p['shares']}股 @ ¥{p['buy_price']:.2f})",
                key="del_single_pos_select",
            )
            if st.button("确认移除", type="primary", key="confirm_del_single_pos"):
                if del_pos_choice and "_id" in del_pos_choice:
                    remove_position(del_pos_choice["_id"])
                    st.toast(f"已移除 {del_pos_choice['ts_code']} 持仓", icon="✅")
                    st.rerun()


def _show_auto_trade():
    """信号自动跟单 - 根据信号中心的最新信号自动更新模拟持仓"""
    st.subheader("📡 信号自动跟单")
    st.caption("根据信号中心的最新买入/卖出信号，自动生成模拟交易建议")

    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        auto_days = st.selectbox("取最近N天的信号", [1, 3, 5, 7], index=0)
    with col2:
        min_score = st.slider("最低信号评分", 0.0, 1.0, 0.6, 0.05)
    with col3:
        st.markdown("")  # 对齐
        refresh_clicked = st.button("🔄 刷新信号", type="secondary", width="stretch")

    # 获取最近的信号（通过 start_date 在数据库层面精准过滤）
    trade_date = (datetime.now() - timedelta(days=auto_days)).strftime("%Y%m%d")
    signals_df = get_signals(start_date=trade_date, limit=200)

    if signals_df.empty:
        st.info("暂无信号数据，请先在信号中心运行扫描")
        return

    # 过滤最低评分
    signals_df = signals_df[signals_df["score"] >= min_score]

    if signals_df.empty:
        st.info(f"最近{auto_days}天无评分≥{min_score}的信号")
        return

    stock_df = get_instrument_list()
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
                "策略": format_strategy_cn(sig["strategy"]),
                "状态": has_pos,
                "原因": sig.get("reason", ""),
            })

        df_buy = pd.DataFrame(buy_rows)
        chinese_dataframe(df_buy)

        # 一键跟单按钮（顶层无嵌套，点击绝不闪退）
        if st.button("📥 一键跟入（将买入信号加入持仓）", type="primary", key="btn_auto_buy"):
            added = 0
            existing_set = set(existing_codes)
            seen_codes = set()
            for _, sig in buy_signals.iterrows():
                code = sig["ts_code"]
                if code not in existing_set and code not in seen_codes and sig["price_ref"] > 0:
                    budget_per_stock = 50000
                    shares = int(budget_per_stock / sig["price_ref"]) // 100 * 100
                    if shares >= 100:
                        add_position(code, sig["price_ref"], shares, sig["trade_date"])
                        added += 1
                        existing_set.add(code)
                        seen_codes.add(code)
            st.toast(f"✅ 已成功跟入 {added} 只标的到模拟持仓", icon="📥")
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
                "策略": format_strategy_cn(sig["strategy"]),
                "持仓": f"{pos['shares']}股" if pos else "无",
                "原因": sig.get("reason", ""),
            })

        df_sell = pd.DataFrame(sell_rows)
        chinese_dataframe(df_sell)

        if st.button("📤 一键跟出（将卖出信号从持仓移除）", type="secondary", key="btn_auto_sell"):
            removed = 0
            target_codes = set(sell_signals["ts_code"].tolist())
            current_positions = get_positions()
            for pos in current_positions:
                if pos["ts_code"] in target_codes:
                    remove_position(pos["_id"])
                    removed += 1
            st.toast(f"✅ 已移除 {removed} 只持仓", icon="📤")
            st.rerun()
    else:
        st.info("无卖出信号")

    # 显示当前持仓的买卖点标注在K线上
    st.subheader("📈 持仓K线（含买卖点标注）")
    current_positions = get_positions()
    if current_positions:
        stock_df = cached_instrument_list()
        pos_name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if stock_df is not None and not stock_df.empty else {}
        pos_code = st.selectbox(
            "选择持仓股票查看K线",
            [p["ts_code"] for p in current_positions],
            format_func=lambda c: f"{c} {pos_name_map.get(c, '')}".strip(),
            key="pos_kline_select",
        )
        if pos_code:
            df = get_daily(pos_code)
            if not df.empty:
                df = apply_indicators(df, ["ma", "vol_ma"])

                # 收集该股票的买入日期（买卖点标注）
                buy_dates = [
                    p["buy_date"] for p in current_positions
                    if p["ts_code"] == pos_code and p.get("buy_date")
                ]
                pos_display_name = pos_name_map.get(pos_code, pos_code)
                fig = create_candlestick_chart(
                    df, pos_code, pos_display_name,
                    show_volume=True, show_macd=False, show_boll=False,
                    buy_dates=buy_dates,
                    height=420,
                )
                st.plotly_chart(fig, width='stretch')
    else:
        st.info("暂无持仓，可通过信号跟单或手动添加")

