"""持仓管理页面 - 自选股 + 模拟持仓 + 信号自动跟单"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta

from data.storage import (get_watchlist, add_to_watchlist, remove_from_watchlist,
                          get_stock_list, get_daily, get_signals, get_latest_date,
                          save_daily, get_positions, add_position, remove_position,
                          clear_positions, get_instrument_list, batch_get_latest,
                          clean_non_watchlist_data, align_volume_unit,
                          check_split_dividend_anomaly, upsert_position_buy,
                          reduce_position, get_trade_journal)
from notifier.push import calc_atr_stops_and_sizing
from data.fetcher import fetch_daily, fetch_instrument_daily
from data.cleaner import clean_daily
from core.config import DATA_START_DATE
from app.st_utils import chinese_dataframe, chinese_date_picker, cached_instrument_list, format_strategy_cn, format_stock_cn
from app.data_viewer import create_candlestick_chart
from data.indicators import apply_indicators
from strategies import STRATEGY_REGISTRY
from engine.scanner import scan_signals
from services.validators import normalize_stock_code
from services.strategy_recommender import evaluate_stock_strategy, batch_update_watchlist_recommendations


def show():
    st.title("💼 持仓管理")

    tab1, tab2, tab3, tab4 = st.tabs([
        "自选股", "模拟持仓", "信号自动跟单", "🧱 核心-卫星与估值配置"
    ])

    with tab1:
        _show_watchlist()
    with tab2:
        _show_portfolio()
    with tab3:
        _show_auto_trade()
    with tab4:
        _show_allocation_and_valuation()


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
                parts = selected.split(" ")
                sel_code = parts[1] if len(parts) >= 2 else parts[0]
                sel_match = filtered[filtered["ts_code"] == sel_code]
                sel_row = sel_match.iloc[0] if not sel_match.empty else None
                sel_name = sel_row["name"] if sel_row is not None else ""
                sel_ind = sel_row.get("industry", "") if sel_row is not None else ""

                # 智能策略分析卡片（实时诊断标的性格与匹配策略）
                eval_rec = evaluate_stock_strategy(sel_code, name=sel_name, industry=sel_ind)
                if eval_rec.get("is_backtested"):
                    st.info(f"💡 **AI 策略适配诊断**：历史回测验证该标的最匹配 **{eval_rec['tag']}**\n\n> 📌 {eval_rec['reason']}")
                else:
                    st.info(f"💡 **AI 策略适配建议**：{eval_rec['reason']} 添加并下载数据后系统将自动进行深度回测校准。")

                download_data = st.checkbox("同时下载历史数据并自动回测匹配策略", value=True, key="wl_download",
                                            help="勾选后将自动下载近5年的历史数据、全策略回测匹配最佳策略标签并扫描最新信号")
            with col_b:
                if st.button("✅ 确认添加", key="wl_add"):
                    parts = selected.split(" ")
                    ts_code = parts[1] if len(parts) >= 2 else parts[0]
                    rec_tag = eval_rec["tag"]

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
                                    if not df.empty:
                                        df = align_volume_unit(ts_code, df)
                                        if check_split_dividend_anomaly(ts_code, df):
                                            from services.data_service import resync_full_history
                                            data_ready = resync_full_history(ts_code, end)
                                        else:
                                            save_daily(df)
                                            data_ready = True
                                    if data_ready:
                                        from engine.backtester import clear_backtester_cache
                                        st.cache_data.clear()
                                        clear_backtester_cache()
                                        st.toast(f"✅ {selected} 数据已就绪 ({len(df)} 条)", icon="📊")
                                    else:
                                        st.warning(f"{selected} 历史数据校验未通过，保留已有数据")
                                else:
                                    st.warning(f"{selected} 暂无可用数据，稍后可通过更新获取")

                        # 数据就绪后，执行全策略回测校准最佳策略
                        if data_ready or (latest and latest >= end):
                            with st.spinner(f"🎯 正在深度分析 {selected} 的最佳策略..."):
                                deep_rec = evaluate_stock_strategy(ts_code, name=sel_name, industry=sel_ind)
                                rec_tag = deep_rec["tag"]
                                st.toast(f"🎯 策略匹配完成：{selected} 最适合【{deep_rec['strategy_cn']}】", icon="🏆")

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

                    add_to_watchlist(ts_code, st.session_state.wl_note, recommended_strategy=rec_tag)
                    st.success(f"已添加 {selected}，匹配策略：{rec_tag}")
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

    # 分组筛选与智能策略重新评估 / ETF 轮动池导入
    from data.storage import get_watchlist_groups, update_watchlist_group
    from strategies.momentum_rotation import ROTATION_ETF_PRESETS
    groups = get_watchlist_groups()
    col_f1, col_f2, col_f3 = st.columns([2.2, 1.1, 1.3])
    with col_f1:
        group_filter = st.selectbox("按分组筛选", ["全部", *groups], key="wl_group_filter")
        if group_filter != "全部":
            watchlist = watchlist[watchlist["group_name"] == group_filter]
    with col_f2:
        st.markdown("")
        if st.button("🎯 重新评估策略", key="btn_refresh_rec", help="重新对当前自选股进行全策略样本外回测与熔断校准", width="stretch"):
            with st.spinner("正在智能评估自选股最佳策略..."):
                updated_count = batch_update_watchlist_recommendations(force=True)
                st.cache_data.clear()
                st.toast(f"✅ 已成功更新 {updated_count} 只自选股的推荐策略", icon="🎯")
                st.rerun()
    with col_f3:
        st.markdown("")
        if st.button("🌐 导入ETF轮动池", key="btn_import_etf_pool", help="一键添加7只低相关多资产ETF（宽基/红利/黄金/纳指/国债）至「ETF轮动池」分组并拉取历史日线", width="stretch"):
            end = datetime.now().strftime("%Y%m%d")
            added_cnt = 0
            with st.spinner("⏳ 正在导入多资产 ETF 轮动池并拉取行情..."):
                for etf_code, etf_name in ROTATION_ETF_PRESETS:
                    latest = get_latest_date(etf_code)
                    if not latest or latest < end:
                        df_etf = fetch_instrument_daily(etf_code, DATA_START_DATE, end)
                        if df_etf is not None and not df_etf.empty:
                            df_etf = clean_daily(df_etf)
                            if not df_etf.empty:
                                df_etf = align_volume_unit(etf_code, df_etf)
                                save_daily(df_etf)
                    add_to_watchlist(
                        etf_code,
                        note=f"多资产轮动:{etf_name}",
                        group_name="ETF轮动池",
                        recommended_strategy="🔄 动量轮动 (多资产横截面轮动)",
                    )
                    added_cnt += 1
            from engine.backtester import clear_backtester_cache
            st.cache_data.clear()
            clear_backtester_cache()
            st.toast(f"✅ 已导入 {added_cnt} 只多资产 ETF 至「ETF轮动池」", icon="🌐")
            st.rerun()

    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}

    # 批量获取所有自选股的最新1条日线，消除逐只全量拉取的 N+1 性能瓶颈
    wl_codes = watchlist["ts_code"].tolist()
    latest_batch_df = batch_get_latest(wl_codes, limit=1)
    latest_by_code = {}
    if not latest_batch_df.empty:
        for _, l_row in latest_batch_df.iterrows():
            latest_by_code[l_row["ts_code"]] = l_row

    rows = []
    for _, wl_row in watchlist.iterrows():
        ts_code = wl_row["ts_code"]
        rec_tag = wl_row.get("recommended_strategy") or "-"
        latest = latest_by_code.get(ts_code)
        if latest is None:
            rows.append({
                "股票": format_stock_cn(ts_code, name_map),
                "最新价": "-",
                "涨跌幅": "-",
                "推荐策略": rec_tag,
                "分组": wl_row.get("group_name", ""),
                "备注": wl_row.get("note", ""),
            })
            continue

        rows.append({
            "股票": format_stock_cn(ts_code, name_map),
            "最新价": f"¥{latest['close']:.2f}",
            "涨跌幅": f"{latest.get('pct_chg', 0):.2f}%",
            "推荐策略": rec_tag,
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
            clean_cache_on_rm = st.checkbox(
                "同时彻底清除这些标的的本地K线与历史信号缓存（彻底移出回测与扫描候选池）",
                value=False,
                key="clean_cache_on_rm",
                help="若勾选，将从本地数据库删除这几只标的的 daily_price 和 signal 历史记录，释放存储空间并让候选池保持清爽；若标的在模拟持仓中，系统将自动受保不予删除。"
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
                    if clean_cache_on_rm:
                        clean_res = clean_non_watchlist_data(remove_codes)
                        st.cache_data.clear()
                        toast_msg = f"✅ 已成功移除 {len(remove_codes)} 只自选股，并彻底清除了 {len(clean_res['deleted_codes'])} 只标的的本地历史行情"
                    else:
                        toast_msg = f"✅ 已成功移除 {len(remove_codes)} 只自选股"
                    st.toast(toast_msg, icon="🗑️")
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



def diagnose_position_action(ts_code: str, buy_price: float, buy_date: str = "", strategy_hint: str = "") -> dict:
    """诊断单只持仓标的的实时风控状态与卖出/止盈指令（委托给 services.position_service）。"""
    from services.position_service import diagnose_position_action as _svc_diagnose
    return _svc_diagnose(
        ts_code=ts_code,
        buy_price=buy_price,
        buy_date=buy_date,
        strategy_hint=strategy_hint,
        get_daily_fn=get_daily,
        get_signals_fn=get_signals,
        calc_atr_fn=calc_atr_stops_and_sizing,
    )


def _show_portfolio():
    """模拟持仓管理（持久化到 SQLite）"""
    st.subheader("模拟持仓")
    st.caption("记录你的实际持仓，实时监控 ATR 动态止损/移动止盈与策略卖出预警（数据持久化到数据库）")

    stock_df = cached_instrument_list()  # 股票 + ETF 合并列表（用于持仓名称显示）
    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}

    # 添加持仓
    with st.form("add_position", clear_on_submit=True):
        st.markdown("**添加持仓**")
        col1, col2, col3, col4, col5 = st.columns([1.1, 1.0, 1.0, 1.1, 1.2])
        with col1:
            code_input = st.text_input("股票代码", placeholder="000001.SZ")
        with col2:
            buy_price = st.number_input("买入价", min_value=0.01, step=0.01)
        with col3:
            shares = st.number_input("数量(股)", min_value=100, step=100)
        with col4:
            buy_date = chinese_date_picker("买入日期", key="buy_date")
        with col5:
            strat_choices = [""] + list(STRATEGY_REGISTRY.keys())
            sel_strat = st.selectbox(
                "建仓策略",
                strat_choices,
                format_func=lambda s: "自动识别" if not s else format_strategy_cn(s),
                key="add_pos_strategy",
            )

        if st.form_submit_button("添加（同股自动合并均价并记入账本）"):
            if code_input and buy_price > 0 and shares > 0:
                code_str = normalize_stock_code(code_input)
                upsert_position_buy(code_str, buy_price, shares, buy_date, note="手动买入", strategy=sel_strat)
                st.rerun()

    positions = get_positions()
    if not positions:
        st.info("暂无持仓记录，请添加")
        return

    # 显示持仓盈亏与风控预警雷达
    rows = []
    diag_by_id = {}
    total_cost = 0
    total_value = 0
    alert_msgs = []

    for pos in positions:
        ts_code = pos["ts_code"]
        diag = diagnose_position_action(ts_code, pos["buy_price"], pos.get("buy_date", ""), strategy_hint=pos.get("strategy", ""))
        if "_id" in pos:
            diag_by_id[pos["_id"]] = diag
        current_price = diag["current_price"]

        cost = pos["buy_price"] * pos["shares"]
        value = current_price * pos["shares"]
        pnl = value - cost
        pnl_pct = diag["pnl_pct"]

        total_cost += cost
        total_value += value

        stock_label = format_stock_cn(ts_code, name_map)
        if diag["level"] in ("danger", "warning"):
            alert_msgs.append(f"**{stock_label}**：{diag['action']}")

        trail_px = diag.get("trailing_stop_price")
        resolved_hint = diag.get("strategy_hint") or pos.get("strategy", "")
        strat_disp = format_strategy_cn(resolved_hint) if resolved_hint else "默认规则"
        rows.append({
            "股票": stock_label,
            "建仓策略": strat_disp,
            "买入价": f"¥{pos['buy_price']:.2f}",
            "现价": f"¥{current_price:.2f}",
            "数量": pos["shares"],
            "市值": f"¥{value:.0f}",
            "盈亏": f"¥{pnl:+.0f}",
            "盈亏%": f"{pnl_pct:+.2f}%",
            "最高浮盈%": f"{diag.get('max_gain_pct', 0.0):+.1f}%",
            "持仓天数": f"{diag.get('holding_days', 0)}天",
            "ATR止损价": f"¥{diag['stop_loss_price']:.2f} (-{diag['sl_pct']:.1f}%)",
            "移动止盈价": f"¥{trail_px:.2f}" if trail_px else "未激活",
            "目标止盈价": f"¥{diag['take_profit_price']:.2f} (+{diag['tp_pct']:.1f}%)",
            "风控处置建议": diag["action"],
        })

    if alert_msgs:
        st.warning("🔔 **持仓风控预警雷达**：\n" + "\n".join(f"- {m}" for m in alert_msgs))

    df = pd.DataFrame(rows)
    chinese_dataframe(df)

    # 汇总
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    total_pnl_sign = "+" if total_pnl > 0 else "-" if total_pnl < 0 else ""
    c1.metric("总成本", f"¥{total_cost:,.0f}")
    c2.metric("总市值", f"¥{total_value:,.0f}")
    c3.metric("总盈亏", f"{total_pnl_sign}¥{abs(total_pnl):,.0f}")
    c4.metric("收益率", f"{total_pnl_pct:+.2f}%")

    col_del1, col_del2, col_del3 = st.columns([1, 1, 1])
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
    with col_del3:
        with st.popover("📤 分批卖出/平仓记账"):
            sell_pos_choice = st.selectbox(
                "选择卖出标的",
                positions,
                format_func=lambda p: f"{p['ts_code']} {name_map.get(p['ts_code'], '')} ({p['shares']}股 @ ¥{p['buy_price']:.2f})",
                key="reduce_pos_select",
            )
            if sell_pos_choice:
                max_sh = int(sell_pos_choice["shares"])
                sell_sh = st.number_input("卖出股数", min_value=1, max_value=max_sh, value=max_sh, step=100, key="reduce_shares")
                cached_diag = diag_by_id.get(sell_pos_choice.get("_id"))
                def_px = cached_diag["current_price"] if cached_diag else float(sell_pos_choice["buy_price"])
                sell_px = st.number_input("成交价格(元)", min_value=0.01, value=round(float(def_px), 2), step=0.01, key="reduce_price")
                sell_dt = chinese_date_picker("卖出日期", key="reduce_date")
                def_strat = str(sell_pos_choice.get("strategy") or (cached_diag.get("strategy_hint") if cached_diag else "") or "")
                sell_strat = st.text_input("关联策略(可选)", value=def_strat, placeholder="如 kdj_cross / risk_manager", key="reduce_strat")
                if st.button("确认平仓记账", type="primary", key="confirm_reduce_pos"):
                    r_out = reduce_position(
                        ts_code=sell_pos_choice["ts_code"],
                        sell_shares=int(sell_sh),
                        sell_price=float(sell_px),
                        sell_date=sell_dt,
                        strategy=sell_strat.strip(),
                        position_id=sell_pos_choice["_id"],
                    )
                    st.toast(f"✅ 已平仓 {r_out['sold_shares']}股，净盈亏 ¥{r_out['pnl']:+.2f} ({r_out['pnl_pct']:+.2f}%)", icon="📒")
                    st.rerun()

    # 实盘交易复盘账本 & 分策略归因
    st.markdown("---")
    with st.expander("📒 实盘交易复盘账本 & 分策略归因", expanded=False):
        from services.position_service import summarize_trade_journal
        j_df = get_trade_journal(limit=300)
        summary = summarize_trade_journal(j_df)
        if j_df.empty:
            st.info("暂无实盘流水记录。可通过上方买入或信号中心一键回填积累实盘归因数据。")
        else:
            jc1, jc2, jc3, jc4 = st.columns(4)
            jc1.metric("已平仓笔数", f"{summary['closed_count']} 笔", f"买入 {summary['buy_count']} 笔")
            jc2.metric("实盘真胜率", f"{summary['win_rate']:.1f}%")
            jc3.metric("累计已实现盈亏", f"¥{summary['total_pnl']:+,.2f}", f"平均 {summary['avg_pnl_pct']:+.2f}%")
            jc4.metric("实盘平均滑点", f"{summary['avg_slippage_pct']:+.3f}%", f"平均持仓 {summary['avg_holding_days']:.1f}天")

            if summary["by_strategy"]:
                st.markdown("**📊 分策略实盘归因**")
                chinese_dataframe(pd.DataFrame(summary["by_strategy"]), height=200)

            st.markdown("**📋 最近实盘成交流水**")
            disp_j = j_df[["trade_date", "ts_code", "direction", "price", "shares", "slippage_pct", "strategy", "pnl", "pnl_pct", "holding_days", "note"]].copy()
            disp_j.columns = ["日期", "代码", "方向", "成交价", "股数", "滑点%", "策略", "净盈亏", "盈亏%", "持仓天", "备注"]
            chinese_dataframe(disp_j, height=280)


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

    buy_signals = signals_df[signals_df["direction"] == "BUY"].copy()
    sell_signals = signals_df[signals_df["direction"] == "SELL"].copy()

    # 冲突保护：若同一标的在同一天或近期同时存在更强/同级的卖出信号，自动从跟单买入建议中剔除
    if not buy_signals.empty and not sell_signals.empty:
        sell_max_by_code = sell_signals.groupby("ts_code")["score"].max().to_dict()
        buy_signals = buy_signals[
            buy_signals.apply(
                lambda r: r["ts_code"] not in sell_max_by_code or r["score"] > sell_max_by_code[r["ts_code"]] + 0.10,
                axis=1,
            )
        ]

    # 按标的去重合并（同一只股票多个策略触发买入时取最高分并合并策略名）
    if not buy_signals.empty:
        buy_signals = buy_signals.sort_values("score", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")

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

        # 一键跟单按钮（按 ATR 风险平价算手数、记录建仓策略并入实盘账本）
        if st.button("📥 一键跟入（将买入信号加入持仓）", type="primary", key="btn_auto_buy"):
            from core.config import DEFAULT_CAPITAL
            added = 0
            existing_set = set(existing_codes)
            seen_codes = set()
            for _, sig in buy_signals.iterrows():
                code = sig["ts_code"]
                ref_px = float(sig.get("price_ref") or 0.0)
                if code not in existing_set and code not in seen_codes and ref_px > 0:
                    strat_k = str(sig.get("strategy") or "").strip()
                    try:
                        plan = calc_atr_stops_and_sizing(
                            code, ref_px, float(sig.get("score") or 0.7),
                            base_capital=float(DEFAULT_CAPITAL), strategy=strat_k,
                        )
                        shares = int(plan.get("shares") or 0)
                    except TypeError:
                        plan = calc_atr_stops_and_sizing(
                            code, ref_px, float(sig.get("score") or 0.7), float(DEFAULT_CAPITAL),
                        )
                        shares = int(plan.get("shares") or 0)
                    if shares < 100:
                        shares = int(float(DEFAULT_CAPITAL) * 0.2 / ref_px) // 100 * 100
                    if shares >= 100:
                        upsert_position_buy(
                            ts_code=code,
                            buy_price=ref_px,
                            shares=shares,
                            buy_date=str(sig.get("trade_date") or datetime.now().strftime("%Y%m%d")),
                            note=f"自动跟单:{strat_k}",
                            signal_price=ref_px,
                            strategy=strat_k,
                        )
                        added += 1
                        existing_set.add(code)
                        seen_codes.add(code)
            st.toast(f"✅ 已按 ATR 风险平价跟入 {added} 只标的并写入账本", icon="📥")
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

        if st.button("📤 一键跟出（平仓卖出信号持仓并记入账本）", type="secondary", key="btn_auto_sell"):
            removed = 0
            total_pnl = 0.0
            sell_best = sell_signals.sort_values("score", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            sell_info_map = {str(r["ts_code"]): r for _, r in sell_best.iterrows()}
            current_positions = get_positions()
            for pos in current_positions:
                p_code = pos["ts_code"]
                if p_code in sell_info_map:
                    s_row = sell_info_map[p_code]
                    s_px = float(s_row.get("price_ref") or 0.0)
                    if s_px <= 0:
                        d_lat = get_daily(p_code, limit=5)
                        s_px = float(d_lat.iloc[-1]["close"]) if (d_lat is not None and not d_lat.empty) else float(pos["buy_price"])
                    res_red = reduce_position(
                        ts_code=p_code,
                        sell_shares=int(pos["shares"]),
                        sell_price=s_px,
                        sell_date=str(s_row.get("trade_date") or datetime.now().strftime("%Y%m%d")),
                        strategy=str(s_row.get("strategy") or pos.get("strategy") or ""),
                        signal_price=s_px,
                        note=f"自动跟出:{s_row.get('strategy', '')}",
                        position_id=pos.get("_id"),
                    )
                    total_pnl += float(res_red.get("pnl", 0.0))
                    removed += 1
            st.toast(f"✅ 已平仓 {removed} 只持仓，累计实现盈亏 ¥{total_pnl:+.2f}", icon="📤")
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


def _show_allocation_and_valuation():
    """核心-卫星70/30仓位规划与基本面估值快照"""
    st.subheader("🧱 核心-卫星仓位配置与估值守卫")
    st.caption("按滚动 60 日波动率(ATR%)、低估高息特征及样本外适配结论，将资金自动划分为「核心底仓 + 卫星趋势」两层，避免高波动股与低波蓝筹混用同一风控与仓位。")

    watchlist = get_watchlist()
    if watchlist.empty:
        st.info("暂无自选股，请先在「自选股」标签页添加标的")
        return

    from services.valuation_service import (
        fetch_and_cache_valuations, build_core_satellite_plan,
    )
    from data.storage import get_valuation_map

    col_c1, col_c2, col_c3 = st.columns([1.4, 1.4, 1.2])
    with col_c1:
        total_cap = st.number_input("总规划资金 (元)", value=100000, min_value=10000, step=10000, key="alloc_cap")
    with col_c2:
        core_pct = st.slider("核心底仓占比 (%)", min_value=30, max_value=90, value=70, step=5, key="alloc_core_pct")
    with col_c3:
        st.markdown("")
        if st.button("🔄 刷新估值数据 (PE/PB/股息率)", key="btn_refresh_val", width="stretch"):
            with st.spinner("正在拉取并缓存自选股最新估值数据..."):
                fetch_and_cache_valuations(watchlist["ts_code"].tolist())
                st.cache_data.clear()
                st.toast("✅ 估值快照更新完成", icon="💎")
                st.rerun()

    val_map = get_valuation_map(watchlist["ts_code"].tolist())
    inst_df = cached_instrument_list()
    plan = build_core_satellite_plan(
        watchlist_df=watchlist,
        capital=float(total_cap),
        core_ratio=float(core_pct) / 100.0,
        val_map=val_map,
        inst_df=inst_df,
    )

    st.info(f"📊 **配置方案摘要**：{plan['summary']}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🧱 核心底仓预算", f"¥{plan['core_budget']:,.0f}", f"{plan['core_count']} 只")
    m2.metric("⚡ 卫星趋势预算", f"¥{plan['satellite_budget']:,.0f}", f"{plan['satellite_count']} 只")
    m3.metric("⛔ 观望空仓数", f"{plan['skip_count']} 只")
    m4.metric("💵 整手后预留现金", f"¥{plan['cash_reserve']:,.0f}")

    if plan["rows"]:
        df_plan = pd.DataFrame(plan["rows"])
        chinese_dataframe(df_plan, height=440)
        csv_plan = df_plan.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 导出核心-卫星配置方案 CSV",
            data=csv_plan,
            file_name=f"核心卫星配置方案_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )

    if val_map:
        st.markdown("---")
        st.markdown("**💎 已缓存估值快照明细**")
        name_map = dict(zip(inst_df["ts_code"], inst_df["name"])) if (inst_df is not None and not inst_df.empty) else {}
        val_rows = []
        for code, v in sorted(val_map.items()):
            val_rows.append({
                "代码": code,
                "名称": name_map.get(code, ""),
                "PE(TTM)": round(v["pe_ttm"], 2) if v.get("pe_ttm") is not None else "-",
                "PE分位%": f"{v['pe_percentile']:.0f}%" if v.get("pe_percentile") is not None else "-",
                "PB": round(v["pb"], 2) if v.get("pb") is not None else "-",
                "股息率%": round(v["dividend_yield"], 2) if v.get("dividend_yield") is not None else "-",
                "总市值(亿)": round(v["market_cap_yi"], 1) if v.get("market_cap_yi") is not None else "-",
                "更新时间": str(v.get("updated_at") or "")[:10],
            })
        chinese_dataframe(pd.DataFrame(val_rows), height=280)

