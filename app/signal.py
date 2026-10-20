"""信号中心页面"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
from datetime import datetime, date

from engine.scanner import scan_signals
from data.storage import get_signals, get_instrument_list, get_daily, get_stocks_with_data, get_watchlist, get_positions
from app.st_utils import (
    chinese_dataframe, chinese_date_picker, strategy_label,
    cached_get_stocks_with_data, cached_instrument_list,
    format_strategy_cn, format_direction_cn, format_stock_cn,
)
from strategies import STRATEGY_REGISTRY
from notifier.push import notify_signals


def show():
    st.title("📡 信号中心")

    tab1, tab2, tab3, tab4 = st.tabs(["实时扫描", "历史信号", "信号验证", "🎯 组合信号"])

    with tab1:
        _show_scan()
    with tab2:
        _show_history()
    with tab3:
        _show_signal_validation()
    with tab4:
        _show_composite()


def _show_scan():
    """实时信号扫描"""
    st.subheader("今日信号扫描")

    # 大盘环境风控横幅
    try:
        from services.signal_service import get_market_regime
        regime_info = get_market_regime()
        st.info(
            f"**大盘风控温度计：{regime_info['label']}** ｜ "
            f"**建议总仓位上限：≤ {regime_info['max_position_pct']}%** ｜ "
            f"{regime_info['advice']}"
        )
    except Exception:
        pass

    col1, col2 = st.columns(2)
    with col1:
        strategy_names = list(STRATEGY_REGISTRY.keys())
        selected_strategies = st.multiselect(
            "选择策略", strategy_names, default=strategy_names,
            format_func=lambda x: strategy_label(
                x, STRATEGY_REGISTRY[x].description,
                getattr(STRATEGY_REGISTRY[x], "style", "综合"),
            ),
        )
    with col2:
        scan_date = chinese_date_picker("扫描日期", default_val=date.today(), key="scan_date")
        scan_date_str = scan_date

    with st.expander("🛡️ 流动性与停牌守卫设置 (实盘风控)", expanded=False):
        fcol1, fcol2 = st.columns(2)
        with fcol1:
            filter_suspended = st.checkbox("过滤最新交易日停牌/无成交量标的", value=True)
        with fcol2:
            amt_option = st.selectbox(
                "近5日最低日均成交额门槛",
                ["不限制", "2000 万元", "3000 万元 (推荐)", "5000 万元"],
                index=2,
            )
            amt_map = {"不限制": 0.0, "2000 万元": 20000000.0, "3000 万元 (推荐)": 30000000.0, "5000 万元": 50000000.0}
            min_daily_amt = amt_map.get(amt_option, 0.0)

    # 股票范围
    stocks_with_data = cached_get_stocks_with_data(min_days=60)
    if not stocks_with_data:
        st.warning("暂无足够的行情数据（需至少60条日线）")
        return

    scope = st.radio(
        "扫描范围",
        [f"有数据的股票 ({len(stocks_with_data)}只)", "自选股(快)"],
        horizontal=True,
    )

    if st.button("🔍 开始扫描", type="primary", width='stretch'):
        if not selected_strategies:
            st.error("请至少选择一个策略")
            return

        # 根据扫描范围确定股票列表
        if scope.startswith("自选股"):
            watchlist_df = get_watchlist()
            scan_codes = watchlist_df["ts_code"].tolist() if not watchlist_df.empty else []
            if not scan_codes:
                st.warning("自选股列表为空，请先添加自选股")
                return
            universe = scan_codes
        else:
            universe = stocks_with_data  # 所有有数据的股票

        scan_codes = universe
        total = len(scan_codes)

        progress_bar = st.progress(0)
        status_text = st.empty()

        def on_progress(completed, total_cnt):
            progress_bar.progress(completed / total_cnt)
            status_text.text(f"扫描中 {completed}/{total_cnt}...")

        signals = scan_signals(
            universe=universe,
            strategy_names=selected_strategies,
            end_date=scan_date_str,
            save=True,
            progress_callback=on_progress,
            min_daily_amount=min_daily_amt,
            filter_suspended=filter_suspended,
            force_refresh=True,
        )

        progress_bar.empty()
        status_text.empty()

        if not signals:
            st.info("今日无交易信号（或均已被流动性/停牌风控拦截）")
            return

        st.success(f"扫描完成，发现 {len(signals)} 条有效交易信号")

        # 推送通知（如果配置了推送通道）
        notify_signals(signals, selected_strategies)

        # 分组显示
        buy_signals = [s for s in signals if s.direction == "BUY"]
        sell_signals = [s for s in signals if s.direction == "SELL"]

        # 导出全部信号为 CSV
        if signals:
            export_rows = []
            stock_df = cached_instrument_list()
            stock_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}
            for sig in signals:
                export_rows.append({
                    "ts_code": sig.ts_code,
                    "name": stock_map.get(sig.ts_code, ""),
                    "trade_date": sig.trade_date,
                    "strategy": sig.strategy,
                    "direction": sig.direction,
                    "score": sig.score,
                    "price_ref": sig.price_ref,
                    "reason": sig.reason,
                })
            csv_data = pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 导出原始信号 CSV",
                data=csv_data,
                file_name=f"原始信号_{scan_date_str}_{datetime.now().strftime('%H%M')}.csv",
                mime="text/csv",
            )

        # 实盘批量委托单导出组件
        _render_broker_order_sheet(signals, scan_date_str, stock_map)

        if buy_signals:
            st.subheader(f"🟢 买入信号 ({len(buy_signals)})")
            _show_signal_table(buy_signals[:50])

        if sell_signals:
            st.subheader(f"🔴 卖出信号 ({len(sell_signals)})")
            _show_signal_table(sell_signals[:50])


def build_deduplicated_broker_orders(signals: list, capital: float = 100000.0,
                                     max_pos_pct: float = 20.0, price_offset_pct: float = 0.0,
                                     stop_loss_pct: float = 0.0, stock_map: dict = None,
                                     held_map: dict = None) -> tuple:
    """将原始信号列表去重聚合为安全的单股单笔券商委托单（剔除同股多空冲突与同向重复下单）

    Args:
        stop_loss_pct: 若 <= 0 则自动启用 ATR14 动态止损，否则使用固定百分比
    Returns:
        (order_rows, txt_lines)
    """
    from notifier.push import calc_atr_stops_and_sizing
    stock_map = stock_map or {}
    held_map = held_map or {}
    single_budget = capital * (max_pos_pct / 100.0)

    # 1. 按股票分组识别多空冲突与同向聚合
    sell_by_code = {}
    buy_by_code = {}
    for sig in signals:
        if sig.direction == "SELL":
            sell_by_code.setdefault(sig.ts_code, []).append(sig)
        elif sig.direction == "BUY":
            buy_by_code.setdefault(sig.ts_code, []).append(sig)

    merged_signals = []
    # 处理买入：剔除同日存在同级或更强卖出信号的冲突标的，同股多策略合并为1笔委托
    for code, b_list in buy_by_code.items():
        s_list = sell_by_code.get(code, [])
        best_buy = max(b_list, key=lambda x: x.score)
        if s_list and max(x.score for x in s_list) >= best_buy.score - 0.05:
            continue
        strat_str = "+".join(dict.fromkeys(format_strategy_cn(x.strategy) for x in b_list))
        merged_signals.append((best_buy, strat_str))

    # 处理卖出：同股多策略卖出合并为1笔委托
    for code, s_list in sell_by_code.items():
        best_sell = max(s_list, key=lambda x: x.score)
        strat_str = "+".join(dict.fromkeys(format_strategy_cn(x.strategy) for x in s_list))
        merged_signals.append((best_sell, strat_str))

    # 排序：卖出在前，买入按评分降序
    merged_signals.sort(key=lambda item: (0 if item[0].direction == "SELL" else 1, -item[0].score))

    order_rows = []
    txt_lines = []
    for sig, strat_str in merged_signals:
        name = stock_map.get(sig.ts_code, "")
        symbol = sig.ts_code.split(".")[0]
        ref_price = sig.price_ref if sig.price_ref > 0 else 10.0

        if sig.direction == "BUY":
            order_price = round(ref_price * (1 + price_offset_pct / 100.0), 2)
            shares = int(single_budget / (order_price * 1.0003)) // 100 * 100
            est_amount = round(shares * order_price, 2)
            if stop_loss_pct <= 0:
                atr_plan = calc_atr_stops_and_sizing(sig.ts_code, order_price, sig.score, capital)
                stop_price = atr_plan["stop_loss"]
                sl_tag = f"¥{stop_price:.2f} (-{atr_plan['sl_pct']:g}% ATR)"
            else:
                stop_price = round(order_price * (1 - stop_loss_pct / 100.0), 2)
                sl_tag = f"¥{stop_price:.2f} (-{stop_loss_pct:g}%)"
            action_text = "买入"
        else:
            order_price = round(ref_price * (1 - price_offset_pct / 100.0), 2)
            shares = held_map.get(sig.ts_code, 100)
            est_amount = round(shares * order_price, 2)
            sl_tag = "-"
            action_text = "卖出"

        order_rows.append({
            "证券代码": symbol,
            "标的代码": sig.ts_code,
            "名称": name,
            "操作": action_text,
            "评分": round(sig.score, 2),
            "委托价格": order_price,
            "委托数量(股)": shares,
            "预估金额(元)": est_amount,
            "建议止损价": sl_tag,
            "来源策略": strat_str,
            "状态": "待委托" if shares > 0 else "资金不足1手",
        })

        if shares > 0:
            txt_lines.append(f"{symbol}\t{shares}\t{order_price:.2f}\t{action_text}")

    return order_rows, txt_lines


def _render_broker_order_sheet(signals: list, scan_date_str: str, stock_map: dict):
    """渲染券商实盘批量委托单（Broker-Ready Order Sheet）"""
    with st.expander("💼 实盘批量委托单生成器 (已开启同股去重防超买保护)", expanded=True):
        st.caption("自动剔除多空分歧股、将同一标的的多策略信号合并为单笔委托，防止导入券商时同股重复下单超买")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            capital = st.number_input("可用交易资金 (元)", value=100000, step=10000, min_value=1000)
        with col2:
            max_pos_pct = st.slider("单票资金上限 (%)", min_value=5, max_value=100, value=20, step=5)
        with col3:
            price_offset_pct = st.number_input("委托加价/折价 (%)", value=0.0, step=0.1, help="买入加价提高成交率，卖出折价确保离场")
        with col4:
            stop_loss_pct = st.number_input("硬止损% (0=ATR动态止损)", value=0.0, step=0.5, min_value=0.0)

        held_map = {p["ts_code"]: p["shares"] for p in get_positions() if p.get("shares", 0) > 0}
        order_rows, txt_lines = build_deduplicated_broker_orders(
            signals=signals,
            capital=float(capital),
            max_pos_pct=float(max_pos_pct),
            price_offset_pct=float(price_offset_pct),
            stop_loss_pct=float(stop_loss_pct),
            stock_map=stock_map,
            held_map=held_map,
        )

        order_df = pd.DataFrame(order_rows)
        chinese_dataframe(order_df)

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            csv_bytes = order_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 导出通用券商批量委托 CSV (含止损风控)",
                data=csv_bytes,
                file_name=f"券商批量委托单_{scan_date_str}_{datetime.now().strftime('%H%M')}.csv",
                mime="text/csv",
            )
        with btn_col2:
            txt_content = "\r\n".join(txt_lines).encode("gbk", errors="ignore")
            st.download_button(
                "📥 导出通达信/同花顺批量委托 TXT",
                data=txt_content,
                file_name=f"券商批量委托_{scan_date_str}.txt",
                mime="text/plain",
            )



def _show_signal_table(signals):
    """显示信号表格"""
    stock_df = cached_instrument_list()
    stock_map = {}
    if not stock_df.empty:
        stock_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

    rows = []
    for sig in signals:
        name = stock_map.get(sig.ts_code, "")
        rows.append({
            "股票": format_stock_cn(sig.ts_code, stock_map),
            "代码": sig.ts_code,
            "名称": name,
            "策略": format_strategy_cn(sig.strategy),
            "方向": format_direction_cn(sig.direction),
            "评分": sig.score,
            "参考价": f"¥{sig.price_ref:.2f}",
            "原因": sig.reason,
        })
    df = pd.DataFrame(rows)
    chinese_dataframe(df)


def _show_history():
    """历史信号查询"""
    st.subheader("历史信号查询")

    col1, col2, col3 = st.columns(3)
    with col1:
        strategy_filter = st.selectbox(
            "策略筛选", ["全部"] + list(STRATEGY_REGISTRY.keys()),
            format_func=lambda x: "全部" if x == "全部" else f"{format_strategy_cn(x)} ({x})",
        )
    with col2:
        direction_filter = st.selectbox(
            "方向筛选", ["全部", "BUY", "SELL"],
            format_func=lambda x: "全部" if x == "全部" else ("买入" if x == "BUY" else "卖出"),
        )
    with col3:
        limit = st.number_input("显示条数", value=50, min_value=10, max_value=500)

    strategy = "" if strategy_filter == "全部" else strategy_filter
    signals_df = get_signals(strategy=strategy, limit=limit)

    if signals_df.empty:
        st.info("暂无信号记录，请先运行信号扫描")
        return

    if direction_filter != "全部":
        signals_df = signals_df[signals_df["direction"] == direction_filter]

    # 格式化显示
    if not signals_df.empty:
        stock_df = cached_instrument_list()
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}
        display_df = signals_df[["trade_date", "ts_code", "strategy",
                                  "direction", "score", "price_ref", "reason"]].copy()
        display_df["ts_code"] = display_df["ts_code"].apply(lambda c: format_stock_cn(c, name_map))
        display_df["strategy"] = display_df["strategy"].apply(format_strategy_cn)
        display_df["direction"] = display_df["direction"].apply(format_direction_cn)
        display_df = display_df.rename(columns={
            "trade_date": "日期", "ts_code": "股票", "strategy": "策略",
            "direction": "方向", "score": "评分", "price_ref": "参考价", "reason": "原因",
        })
        chinese_dataframe(display_df)

        # 导出 CSV
        csv_data = display_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 导出历史信号 CSV",
            data=csv_data,
            file_name=f"历史信号_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )


def _show_signal_validation():
    """信号回测验证 - 信号发布后N日涨跌幅统计"""
    st.subheader("📊 信号回测验证")
    st.caption("按实盘「次日开盘买入」或「当日收盘」统计历史信号发出后N个交易日的真实胜率与期望收益，自动识别高胜率策略")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        strategy_filter = st.selectbox(
            "策略筛选", ["全部"] + list(STRATEGY_REGISTRY.keys()),
            key="sv_strategy",
            format_func=lambda x: "全部" if x == "全部" else f"{format_strategy_cn(x)} ({x})",
        )
    with col2:
        direction_filter = st.selectbox(
            "方向筛选", ["全部", "BUY", "SELL"], index=1, key="sv_dir",
            format_func=lambda x: "全部" if x == "全部" else ("买入" if x == "BUY" else "卖出"),
        )
    with col3:
        hold_days = st.selectbox("持有天数", [1, 3, 5, 10, 20], index=2, key="sv_days")
    with col4:
        exec_price_mode = st.selectbox(
            "买入基准价",
            ["次日开盘价 (实盘推荐)", "信号当日收盘价"],
            index=0,
            key="sv_exec_mode",
        )
    skip_high_open = st.checkbox("实盘风控过滤：次日跳空高开 > 3% 不追高", value=True, key="sv_skip_gap")

    strategy = "" if strategy_filter == "全部" else strategy_filter
    signals_df = get_signals(strategy=strategy, limit=500)

    if signals_df.empty:
        st.info("暂无信号记录")
        return

    if direction_filter != "全部":
        signals_df = signals_df[signals_df["direction"] == direction_filter]

    if signals_df.empty:
        st.info("无匹配的信号记录")
        return

    stock_df = get_instrument_list()
    name_map = {}
    if not stock_df.empty:
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

    use_next_open = "次日开盘" in exec_price_mode
    results = []
    stock_cache = {}
    with st.spinner("正在验证信号..."):
        for _, sig in signals_df.iterrows():
            ts_code = sig["ts_code"]
            trade_date = sig["trade_date"]
            direction = sig["direction"]

            # 获取信号发布后的行情（内存字典缓存，避免500次重复DB查询）
            if ts_code not in stock_cache:
                stock_cache[ts_code] = get_daily(ts_code)
            df = stock_cache[ts_code]
            if df.empty:
                continue

            # 找到信号日期在数据中的整数位置
            matches = (df["trade_date"] == trade_date).to_numpy().nonzero()[0]
            if len(matches) == 0:
                continue

            pos = int(matches[0])
            signal_close = float(df.iloc[pos]["close"])

            if use_next_open:
                if pos + 1 >= len(df):
                    continue
                entry_price = float(df.iloc[pos + 1]["open"])
                if entry_price <= 0:
                    entry_price = signal_close
                # 跳空高开超 3% 不追高过滤
                if skip_high_open and direction == "BUY" and signal_close > 0:
                    gap_pct = (entry_price / signal_close - 1) * 100
                    if gap_pct > 3.0:
                        continue
            else:
                entry_price = signal_close

            # N日后的价格
            future_idx = pos + hold_days
            if future_idx >= len(df):
                continue

            future_price = float(df.iloc[future_idx]["close"])

            # 计算涨跌幅
            if direction == "BUY":
                ret = (future_price / entry_price - 1) * 100
                is_good = ret > 0
            else:
                ret = (1 - future_price / entry_price) * 100
                is_good = ret > 0

            results.append({
                "日期": trade_date,
                "股票": f"{ts_code} {name_map.get(ts_code, '')}",
                "方向": format_direction_cn(direction),
                "策略代码": sig["strategy"],
                "策略": format_strategy_cn(sig["strategy"]),
                "评分": round(float(sig.get("score", 0)), 2),
                "基准买入价": round(entry_price, 2),
                f"{hold_days}日后价": round(future_price, 2),
                f"{hold_days}日收益%": round(ret, 2),
                "有效": "✅" if is_good else "❌",
            })

    if not results:
        st.warning("数据不足以验证信号（最新交易日的信号需等待后续K线走出后方可验证）")
        return

    df_results = pd.DataFrame(results)

    # 统计指标
    total = len(df_results)
    win_count = (df_results["有效"] == "✅").sum()
    win_rate = win_count / total * 100
    avg_return = df_results[f"{hold_days}日收益%"].mean()
    max_return = df_results[f"{hold_days}日收益%"].max()
    min_return = df_results[f"{hold_days}日收益%"].min()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("已验证信号数", total)
    col2.metric("真实胜率", f"{win_rate:.1f}%")
    col3.metric("平均收益", f"{avg_return:+.2f}%")
    col4.metric("最佳/最差", f"{max_return:+.1f}% / {min_return:+.1f}%")

    # 新增：按策略维度的真实胜率排行榜
    st.subheader(f"🏆 策略真实表现排行榜（持有 {hold_days} 日 · {'次日开盘成交' if use_next_open else '当日收盘成交'}）")
    strat_grp = df_results.groupby("策略").agg(
        信号数=("有效", "count"),
        胜率=("有效", lambda s: round((s == "✅").mean() * 100, 1)),
        平均收益=(f"{hold_days}日收益%", lambda s: round(s.mean(), 2)),
        中位收益=(f"{hold_days}日收益%", lambda s: round(s.median(), 2)),
        最佳收益=(f"{hold_days}日收益%", lambda s: round(s.max(), 2)),
        最差收益=(f"{hold_days}日收益%", lambda s: round(s.min(), 2)),
    ).reset_index()
    strat_grp["评级建议"] = strat_grp.apply(
        lambda r: "🟢 主力推荐" if (r["胜率"] >= 55 and r["平均收益"] > 0)
        else ("🔴 建议规避" if (r["胜率"] < 40 and r["平均收益"] < 0) else "🟡 谨慎参考"),
        axis=1,
    )
    strat_grp = strat_grp.sort_values(["胜率", "平均收益"], ascending=False).reset_index(drop=True)
    chinese_dataframe(strat_grp)

    # 显示详情
    st.subheader("📋 信号验证明细")
    display_details = df_results.drop(columns=["策略代码"], errors="ignore")
    chinese_dataframe(display_details)

    # 导出
    csv = df_results.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 导出CSV",
        data=csv,
        file_name=f"信号验证_{hold_days}日_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )


def _show_composite():
    """组合信号合成 - 多策略共识分析"""
    st.subheader("🎯 组合信号合成")
    st.caption("综合多个策略的信号，策略共识越强，信号可信度越高")

    # 获取最近N天的信号
    days = st.selectbox("分析最近N天的信号", [1, 3, 5, 7], index=0, key="comp_days")
    min_score = st.slider("最低信号评分", 0.0, 1.0, 0.5, 0.05, key="comp_score")

    if not st.button("🎯 分析组合信号", type="primary"):
        st.info("点击「分析组合信号」查看多策略共识结果")
        return

    trade_date = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    signals_df = get_signals(start_date=trade_date, limit=1000)

    if signals_df.empty:
        st.info("暂无信号数据，请先在实时扫描中运行扫描")
        return

    # 过滤
    signals_df = signals_df[signals_df["score"] >= min_score]

    if signals_df.empty:
        st.info(f"最近{days}天无评分≥{min_score}的信号")
        return

    stock_df = get_instrument_list()
    name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

    # 按股票+方向聚合
    composite = signals_df.groupby(["ts_code", "direction"]).agg(
        strategy_count=("strategy", "nunique"),
        strategies=("strategy", lambda x: ", ".join(sorted(set(x)))),
        avg_score=("score", "mean"),
        max_score=("score", "max"),
        signal_count=("score", "count"),
    ).reset_index()

    # 共识强度
    composite["共识强度"] = composite["strategy_count"].apply(
        lambda x: "🔴 强共识" if x >= 4 else ("🟡 中共识" if x >= 2 else "⚪ 单策略")
    )
    max_count = max(float(composite["signal_count"].max()), 1.0)
    composite["合成评分"] = (
        composite["avg_score"] * 0.6 + (composite["signal_count"] / max_count) * 0.4
    ).round(2)

    # 识别存在多空冲突的股票（防止一边看空一边进入买入共识）
    dir_by_code = signals_df.groupby("ts_code")["direction"].apply(set).to_dict()
    conflicted_codes = {c for c, dirs in dir_by_code.items() if "BUY" in dirs and "SELL" in dirs}

    # 买入共识（自动剔除存在同日/同期看空冲突的标的）
    st.subheader("🟢 买入共识（已剔除多空分歧标的）")
    buy_consensus = composite[
        (composite["direction"] == "BUY") & (~composite["ts_code"].isin(conflicted_codes))
    ].sort_values("合成评分", ascending=False)
    if not buy_consensus.empty:
        buy_rows = []
        for _, row in buy_consensus.iterrows():
            buy_rows.append({
                "股票": f"{row['ts_code']} {name_map.get(row['ts_code'], '')}",
                "共识策略数": f"{row['strategy_count']}个",
                "共识强度": row["共识强度"],
                "合成评分": row["合成评分"],
                "策略": format_strategy_cn(row["strategies"]),
            })
        chinese_dataframe(pd.DataFrame(buy_rows))
    else:
        st.info("无买入共识信号")

    # 卖出共识
    st.subheader("🔴 卖出共识")
    sell_consensus = composite[composite["direction"] == "SELL"].sort_values("合成评分", ascending=False)
    if not sell_consensus.empty:
        sell_rows = []
        for _, row in sell_consensus.iterrows():
            sell_rows.append({
                "股票": f"{row['ts_code']} {name_map.get(row['ts_code'], '')}",
                "共识策略数": f"{row['strategy_count']}个",
                "共识强度": row["共识强度"],
                "合成评分": row["合成评分"],
                "策略": format_strategy_cn(row["strategies"]),
            })
        chinese_dataframe(pd.DataFrame(sell_rows))
    else:
        st.info("无卖出共识信号")

    # 策略冲突分析
    st.subheader("⚡ 策略冲突")
    conflict = signals_df.groupby("ts_code").agg(
        directions=("direction", lambda x: list(x)),
    ).reset_index()
    conflict["has_conflict"] = conflict["directions"].apply(
        lambda x: "BUY" in x and "SELL" in x
    )
    conflict = conflict[conflict["has_conflict"]]
    if not conflict.empty:
        conflict_rows = []
        for _, row in conflict.iterrows():
            conflict_rows.append({
                "股票": f"{row['ts_code']} {name_map.get(row['ts_code'], '')}",
                "说明": "部分策略看多、部分看空，注意风险",
            })
        chinese_dataframe(pd.DataFrame(conflict_rows))
    else:
        st.info("无策略冲突")

