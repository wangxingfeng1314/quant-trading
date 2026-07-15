"""选股因子筛选器 - 技术 + 基本面因子筛选股票"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import numpy as np

from data.storage import get_stock_list, get_daily, get_stocks_with_data
from app.st_utils import chinese_dataframe
from data.indicators import apply_indicators


def show():
    st.title("🔍 选股因子筛选器")
    st.caption("按技术指标 + 基本面因子筛选股票，快速定位符合条件的标的")

    stock_df = get_stock_list()
    if stock_df.empty:
        st.warning("暂无股票数据")
        return

    # 上方筛选区
    with st.expander("🎯 筛选条件", expanded=True):
        tab_a, tab_b = st.tabs(["📊 技术因子", "📈 行情因子"])

        with tab_a:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                # 显示实际可扫描的股票数量（只读提示，不再让用户选N只）
                stocks_with_data = get_stocks_with_data(min_days=60)
                max_stocks = len(stocks_with_data)
                st.caption(f"📊 本次可扫描: {max_stocks} 只股票")
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
            col1, col2, col3 = st.columns(3)
            with col1:
                pct_chg_min = st.number_input("今日涨幅 ≥ %", value=-10.0, step=0.5)
            with col2:
                pct_chg_max = st.number_input("今日涨幅 ≤ %", value=10.0, step=0.5)
            with col3:
                turnover_min = st.number_input("换手率 ≥ %", value=0.0, step=0.5)

    # 执行按钮
    run_btn = st.button("🔍 开始筛选", type="primary", width='stretch')

    if not run_btn:
        st.info("请在左侧设置筛选条件后点击「开始筛选」")
        return

    # 执行筛选
    # 只扫描有日线数据的股票（不再从全市场前N只硬编码）
    codes_with_data = get_stocks_with_data(min_days=60)
    if not codes_with_data:
        st.warning("暂无足够的行情数据（需至少60条日线）")
        return

    actual_scan = codes_with_data
    with st.spinner(f"正在扫描 {len(actual_scan)} 只自选股..."):
        codes = actual_scan
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))
        results = []

        for ts_code in codes:
            df = get_daily(ts_code)
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

            # 2. 突破MA20
            if match and breakout_ma20:
                if "ma20" not in df.columns:
                    match = False
                elif not (prev["close"] <= prev["ma20"] < latest["close"]):
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
            if match:
                chg = latest.get("pct_chg", 0)
                if pd.notna(chg):
                    if chg < pct_chg_min or chg > pct_chg_max:
                        match = False
                if match and chg is not None:
                    reasons.append(f"涨{chg:.1f}%" if chg >= 0 else f"跌{chg:.1f}%")

            # 9. 换手率
            if match and turnover_min > 0:
                turnover = latest.get("turnover", 0)
                if pd.notna(turnover) and turnover < turnover_min:
                    match = False

            if match:
                chg = latest.get("pct_chg", 0)
                # 综合评分：因子命中数 × 10 + RSI加分 + MACD加分
                factor_count = len(reasons)
                rsi_score = min(latest.get("rsi14", 50) / 10, 10) if enable_rsi else 5
                score = round(factor_count * 10 + rsi_score, 1)

                results.append({
                    "代码": ts_code,
                    "名称": name_map.get(ts_code, ""),
                    "最新价": latest["close"],
                    "涨跌幅%": round(chg, 2) if pd.notna(chg) else 0,
                    "成交量(万手)": round(latest["volume"] / 10000, 0) if latest["volume"] else 0,
                    "MA5": round(latest.get("ma5", 0), 2),
                    "MA20": round(latest.get("ma20", 0), 2),
                    "MA60": round(latest.get("ma60", 0), 2),
                    "RSI14": round(latest.get("rsi14", 0), 1),
                    "综合评分": score,
                    "命中因子": len(reasons),
                    "因子匹配": "; ".join(reasons),
                })

    if not results:
        st.warning("未找到符合条件的股票，请放宽筛选条件")
        return

    # 综合评分（因子命中数 × 10 + RSI加分）
    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values(["命中因子", "综合评分"], ascending=False).reset_index(drop=True)

    st.success(f"筛选完成，共 {len(df_result)} 只股票符合条件")

    # 指标卡
    col1, col2, col3 = st.columns(3)
    col1.metric("符合条件的股票", len(df_result))
    col2.metric("平均涨幅", f"{df_result['涨跌幅%'].mean():.2f}%")
    col3.metric("平均RSI", f"{df_result['RSI14'].mean():.1f}")

    # 格式化显示
    display = df_result.copy()
    display["最新价"] = display["最新价"].apply(lambda x: f"¥{x:.2f}")
    display["MA5"] = display["MA5"].apply(lambda x: f"¥{x:.2f}")
    display["MA20"] = display["MA20"].apply(lambda x: f"¥{x:.2f}")
    display["MA60"] = display["MA60"].apply(lambda x: f"¥{x:.2f}")
    display["成交量(万手)"] = display["成交量(万手)"].apply(lambda x: f"{x:.0f}")

    chinese_dataframe(display)

    # 导出按钮
    csv = df_result.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 导出CSV",
        data=csv,
        file_name=f"选股结果_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
