"""选股因子筛选器 - 技术 + 行情因子筛选股票/ETF"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import numpy as np

from data.storage import (
    get_instrument_list, get_daily, get_stocks_with_data, batch_get_latest,
    get_valuation_map, add_to_watchlist, get_watchlist,
)
from app.st_utils import chinese_dataframe, cached_get_stocks_with_data, cached_instrument_list
from data.indicators import apply_indicators
from engine.commission import format_price


def _render_screener_results(df_result: pd.DataFrame):
    """渲染选股结果表、导出按钮与一键批量加入自选股区"""
    st.success(f"筛选完成，共 {len(df_result)} 个标的符合条件")

    col1, col2, col3 = st.columns(3)
    col1.metric("符合条件的标的", len(df_result))
    col2.metric("平均涨幅", f"{df_result['涨跌幅%'].mean():.2f}%")
    col3.metric("平均RSI", f"{df_result['RSI14'].mean():.1f}")

    display = df_result.copy()
    for pcol in ("最新价", "MA5", "MA20", "MA60"):
        if pcol in display.columns:
            display[pcol] = display.apply(
                lambda r, col=pcol: format_price(r[col], r.get("代码", "")) if pd.notna(r[col]) and float(r[col]) > 0 else "-",
                axis=1,
            )
    display["成交量(万手)"] = display["成交量(万手)"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "0.00")
    if "PE-TTM" in display.columns:
        display["PE-TTM"] = display["PE-TTM"].apply(lambda x: f"{x:.1f}" if pd.notna(x) else "-")
    if "PE分位%" in display.columns:
        display["PE分位%"] = display["PE分位%"].apply(lambda x: f"{x:.0f}%" if pd.notna(x) else "-")
    if "PB" in display.columns:
        display["PB"] = display["PB"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")

    chinese_dataframe(display)

    csv = df_result.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 导出CSV",
        data=csv,
        file_name=f"筛选结果_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        width="stretch",
    )

    wl_df = get_watchlist()
    existing_wl = (
        set(wl_df["ts_code"].astype(str).tolist())
        if (wl_df is not None and not wl_df.empty and "ts_code" in wl_df.columns)
        else set()
    )
    code_options = df_result["代码"].tolist()
    code_label_map = dict(zip(df_result["代码"], df_result["名称"]))
    default_picks = [c for c in code_options[:5] if c not in existing_wl]

    with st.container(border=True):
        st.markdown("##### ⭐ 批量加入自选股池")
        sc_c1, sc_c2, sc_c3 = st.columns([3, 1.5, 1])
        with sc_c1:
            selected_to_add = st.multiselect(
                "选择要加入自选股的标的",
                options=code_options,
                default=default_picks,
                format_func=lambda c: f"{c} {code_label_map.get(c, '')}{' (已在自选)' if c in existing_wl else ''}",
                key="sc_add_wl_codes",
            )
        with sc_c2:
            target_group = st.text_input("目标分组 (可选)", value="默认", key="sc_add_wl_group")
        with sc_c3:
            st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
            add_wl_clicked = st.button("⭐ 一键加入自选股", type="secondary", width="stretch", key="sc_add_wl_btn")

        if add_wl_clicked:
            if not selected_to_add:
                st.warning("请先选择至少一只标的")
            else:
                added_cnt, skipped_cnt = 0, 0
                for c in selected_to_add:
                    if c in existing_wl:
                        skipped_cnt += 1
                        continue
                    add_to_watchlist(c, group_name=target_group.strip() or "默认")
                    existing_wl.add(c)
                    added_cnt += 1
                if added_cnt > 0:
                    st.success(
                        f"✅ 已将 {added_cnt} 只标的加入自选股「{target_group.strip() or '默认'}」"
                        + (f"（跳过 {skipped_cnt} 只已在自选）" if skipped_cnt else "")
                    )
                else:
                    st.info(f"所选 {skipped_cnt} 只标的均已在自选股中")


def show():
    st.title("🔍 选股因子筛选器")
    st.caption("按技术指标 + 行情因子 + 基本面估值筛选股票/ETF，快速定位符合条件的标的")

    stock_df = cached_instrument_list()
    if stock_df.empty:
        st.warning("暂无标的（股票/ETF）数据")
        return

    # 上方筛选区
    with st.expander("🎯 筛选条件", expanded=True):
        tab_a, tab_b, tab_c = st.tabs(["📊 技术因子", "📈 行情因子", "💰 估值因子"])

        with tab_a:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                # 显示实际可扫描的标数量（只读提示）
                stocks_with_data = cached_get_stocks_with_data(min_days=60)
                max_stocks = len(stocks_with_data)
                st.caption(f"📊 本次可扫描: {max_stocks} 个标的")
                ma_bullish = st.checkbox("均线多头排列 (MA5>MA20>MA60)", value=False)
                breakout_ma20 = st.checkbox("价格突破MA20", value=False)
                macd_golden = st.checkbox("MACD金叉 (DIF上穿DEA)", value=False)
            with col2:
                enable_rsi = st.checkbox("RSI范围筛选", value=False)
                rsi_min = st.slider("RSI最小值", 0, 100, 30, key="rsi_min", disabled=not enable_rsi)
                rsi_max = st.slider("RSI最大值", 0, 100, 70, key="rsi_max", disabled=not enable_rsi)
            with col3:
                vol_surge = st.checkbox("成交量放大 (>VOL_MA5*1.5)", value=False)
                kdj_golden = st.checkbox("KDJ金叉 (K上穿D)", value=False)
            with col4:
                boll_option = st.selectbox(
                    "布林带位置", ["不限制", "突破上轨", "突破下轨", "中轨上方", "中轨下方"],
                )

        with tab_b:
            col0, col1, col2, col3 = st.columns([1.2, 2, 2, 2])
            with col0:
                enable_pct_chg = st.checkbox("涨跌幅筛选", value=False)
            with col1:
                pct_chg_min = st.number_input("今日涨幅 ≥ %", value=-10.0, step=0.5, disabled=not enable_pct_chg)
            with col2:
                pct_chg_max = st.number_input("今日涨幅 ≤ %", value=10.0, step=0.5, disabled=not enable_pct_chg)
            with col3:
                turnover_min = st.number_input("换手率 ≥ %", value=0.0, step=0.5)

        with tab_c:
            st.caption("基于本地 `stock_valuation` 估值表筛选（可在「持仓管理」同步最新 PE-TTM / PB / 3年分位）")
            vc0, vc1, vc2, vc3 = st.columns(4)
            with vc0:
                exclude_loss = st.checkbox("剔除亏损股 (PE-TTM > 0)", value=False, key="sc_ex_loss")
            with vc1:
                pe_max = st.number_input("PE-TTM 上限 (0=不限)", min_value=0.0, max_value=500.0, value=0.0, step=5.0, key="sc_pe_max")
            with vc2:
                pb_max = st.number_input("PB 上限 (0=不限)", min_value=0.0, max_value=50.0, value=0.0, step=0.5, key="sc_pb_max")
            with vc3:
                pe_pct_max = st.number_input("3年 PE 分位上限 % (0=不限)", min_value=0.0, max_value=100.0, value=0.0, step=5.0, key="sc_pe_pct_max")

    # 执行按钮
    run_btn = st.button("🔍 开始筛选", type="primary", width='stretch')

    if not run_btn:
        cached_df = st.session_state.get("screener_results")
        if isinstance(cached_df, pd.DataFrame) and not cached_df.empty:
            _render_screener_results(cached_df)
        else:
            st.info("请在上方设置筛选条件后点击「开始筛选」")
        return

    # 执行筛选
    # 只扫描有日线数据的股票（不再从全市场前N只硬编码）
    codes_with_data = cached_get_stocks_with_data(min_days=60)
    if not codes_with_data:
        st.warning("暂无足够的行情数据（需至少60条日线）")
        return

    actual_scan = codes_with_data
    with st.spinner(f"正在扫描 {len(actual_scan)} 个标的..."):
        codes = actual_scan
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))
        val_map = get_valuation_map(codes)
        results = []

        # 批量一次拉取所有扫描标的最近120条K线，消除 N+1 逐只 SQL 查询瓶颈
        batch_df = batch_get_latest(codes, limit=120)
        grouped_daily = {}
        if not batch_df.empty and "ts_code" in batch_df.columns:
            for code_key, grp in batch_df.groupby("ts_code", sort=False):
                grouped_daily[code_key] = grp.sort_values("trade_date").reset_index(drop=True)

        for ts_code in codes:
            df = grouped_daily.get(ts_code)
            if df is None or df.empty:
                df = get_daily(ts_code, limit=120)
            if df.empty or len(df) < 60:
                continue

            df = apply_indicators(df, ["ma", "macd", "rsi", "boll", "kdj", "vol_ma"])
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest

            match = True
            reasons = []

            # 1. 均线多头排列
            if ma_bullish:
                if not ("ma5" in df.columns and "ma20" in df.columns and "ma60" in df.columns):
                    match = False
                elif not (latest["ma5"] > latest["ma20"] > latest["ma60"]):
                    match = False
                else:
                    reasons.append("均线多头")

            # 2. 突破MA20（昨日收盘在昨日MA20下方，今日收盘突破今日MA20上方）
            if match and breakout_ma20:
                if "ma20" not in df.columns:
                    match = False
                elif not (prev["close"] <= prev["ma20"] and latest["close"] > latest["ma20"]):
                    match = False
                else:
                    reasons.append("突破MA20")

            # 3. MACD金叉
            if match and macd_golden:
                if "dif" not in df.columns or "dea" not in df.columns:
                    match = False
                elif not (prev["dif"] <= prev["dea"] and latest["dif"] > latest["dea"]):
                    match = False
                else:
                    reasons.append("MACD金叉")

            # 4. RSI范围
            if match and enable_rsi:
                rsi_val = latest.get("rsi14", 50)
                if pd.isna(rsi_val) or not (rsi_min <= rsi_val <= rsi_max):
                    match = False
                else:
                    reasons.append(f"RSI={rsi_val:.0f}")

            # 5. 成交量放大
            if match and vol_surge:
                vol_ma5 = latest.get("vol_ma5", 0)
                if vol_ma5 <= 0 or latest["volume"] < vol_ma5 * 1.5:
                    match = False
                else:
                    reasons.append("放量")

            # 6. 布林带
            if match and boll_option != "不限制":
                if "boll_upper" not in df.columns:
                    match = False
                else:
                    if boll_option == "突破上轨" and latest["close"] < latest["boll_upper"]:
                        match = False
                    elif boll_option == "突破下轨" and latest["close"] > latest["boll_lower"]:
                        match = False
                    elif boll_option == "中轨上方" and latest["close"] < latest["boll_mid"]:
                        match = False
                    elif boll_option == "中轨下方" and latest["close"] > latest["boll_mid"]:
                        match = False
                if match:
                    reasons.append(boll_option)

            # 7. KDJ金叉
            if match and kdj_golden:
                if "kdj_k" not in df.columns or "kdj_d" not in df.columns:
                    match = False
                elif not (prev["kdj_k"] <= prev["kdj_d"] and latest["kdj_k"] > latest["kdj_d"]):
                    match = False
                else:
                    reasons.append("KDJ金叉")

            # 8. 涨跌幅
            if match and enable_pct_chg:
                chg = latest.get("pct_chg", 0)
                if pd.notna(chg):
                    if chg < pct_chg_min or chg > pct_chg_max:
                        match = False
                if match and pd.notna(chg):
                    reasons.append(f"涨{chg:.1f}%" if chg >= 0 else f"跌{chg:.1f}%")

            # 9. 换手率
            if match and turnover_min > 0:
                turnover = latest.get("turnover", 0)
                if pd.notna(turnover) and turnover < turnover_min:
                    match = False

            # 10. 基本面估值因子 (PE-TTM / PB / 3年 PE 分位 / 剔除亏损股)
            v_row = val_map.get(ts_code, {})
            pe_val = v_row.get("pe_ttm")
            pe_pct_val = v_row.get("pe_percentile")
            pb_val = v_row.get("pb")
            if match and exclude_loss:
                if pe_val is None or pd.isna(pe_val) or float(pe_val) <= 0:
                    match = False
            if match and pe_max > 0:
                if pe_val is None or pd.isna(pe_val) or float(pe_val) <= 0 or float(pe_val) > pe_max:
                    match = False
            if match and pb_max > 0:
                if pb_val is None or pd.isna(pb_val) or float(pb_val) <= 0 or float(pb_val) > pb_max:
                    match = False
            if match and pe_pct_max > 0:
                if pe_pct_val is None or pd.isna(pe_pct_val) or float(pe_pct_val) > pe_pct_max:
                    match = False
            if match and (exclude_loss or pe_max > 0 or pb_max > 0 or pe_pct_max > 0):
                val_tags = []
                if pe_val is not None and pd.notna(pe_val) and float(pe_val) > 0:
                    val_tags.append(f"PE {float(pe_val):.1f}")
                if pb_val is not None and pd.notna(pb_val) and float(pb_val) > 0:
                    val_tags.append(f"PB {float(pb_val):.2f}")
                if pe_pct_val is not None and pd.notna(pe_pct_val):
                    val_tags.append(f"PE分位 {float(pe_pct_val):.0f}%")
                if val_tags:
                    reasons.append("/".join(val_tags))

            if match:
                chg = latest.get("pct_chg", 0)
                # 综合评分：因子命中数 × 10 + RSI加分 + MACD加分
                factor_count = len(reasons)
                rsi_score = min(latest.get("rsi14", 50) / 10, 10) if enable_rsi else 5
                score = round(factor_count * 10 + rsi_score, 1)

                results.append({
                    "代码": ts_code,
                    "名称": name_map.get(ts_code, ""),
                    "类型": stock_df.loc[stock_df["ts_code"] == ts_code, "type"].iloc[0]
                            if ("type" in stock_df.columns and (stock_df["ts_code"] == ts_code).any()) else "",
                    "最新价": latest["close"],
                    "涨跌幅%": round(chg, 2) if pd.notna(chg) else 0,
                    "PE-TTM": round(float(pe_val), 2) if (pe_val is not None and pd.notna(pe_val)) else None,
                    "PE分位%": round(float(pe_pct_val), 1) if (pe_pct_val is not None and pd.notna(pe_pct_val)) else None,
                    "PB": round(float(pb_val), 2) if (pb_val is not None and pd.notna(pb_val)) else None,
                    "成交量(万手)": round(float(latest["volume"]) / 1e6, 2) if latest.get("volume") else 0.0,
                    "MA5": round(latest.get("ma5", 0), 2),
                    "MA20": round(latest.get("ma20", 0), 2),
                    "MA60": round(latest.get("ma60", 0), 2),
                    "RSI14": round(latest.get("rsi14", 0), 1),
                    "综合评分": score,
                    "命中因子": len(reasons),
                    "因子匹配": "; ".join(reasons),
                })

    if not results:
        st.session_state.pop("screener_results", None)
        st.warning("未找到符合条件的标的，请放宽筛选条件")
        return

    # 综合评分（因子命中数 × 10 + RSI加分）
    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values(["命中因子", "综合评分"], ascending=False).reset_index(drop=True)
    st.session_state["screener_results"] = df_result
    _render_screener_results(df_result)
