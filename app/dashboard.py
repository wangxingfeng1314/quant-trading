"""首页看板 - 大盘概况 + 自选股快照 + 市场情绪"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

from data.storage import (
    get_instrument_list, get_daily, get_watchlist, get_daily_count,
    get_signals, batch_get_latest, get_index_daily, get_stocks_with_data,
    get_latest_date, get_index_latest_date,
)
from app.st_utils import (
    chinese_dataframe, cached_get_stocks_with_data, cached_check_data_freshness,
    cached_instrument_list, format_strategy_cn, format_direction_cn,
)
from data.indicators import apply_indicators
from core.config import TUSHARE_TOKEN




def show():
    st.title("📊 A股量化交易系统")

    # 数据时效提示（使用 UI 级缓存，避免高频全表扫描）
    freshness = cached_check_data_freshness()
    latest_date = freshness.get("latest_date", "")
    if latest_date:
        today = datetime.now().strftime("%Y%m%d")
        days_diff = 0
        try:
            d1 = datetime.strptime(latest_date, "%Y%m%d")
            d2 = datetime.strptime(today, "%Y%m%d")
            days_diff = (d2 - d1).days
        except ValueError:
            pass

        if days_diff <= 1:
            status = "✅ 数据正常"
            help_text = ""
        elif days_diff <= 3:
            status = "⚠️ 数据稍旧"
            help_text = "建议运行增量更新"
        elif days_diff <= 5:
            status = "⚠️ 数据较旧"
            help_text = "请运行 `python scripts/init_data.py --update --days 30`"
        else:
            status = "🔴 数据严重滞后"
            help_text = "请尽快运行数据更新"

        st.caption(f"🕐 数据更新至: **{latest_date}** ({status}) "
                   f"| 有数据股票: {freshness['active_stocks']} 只 "
                   f"| 日线总量: {freshness['total_rows']:,} 条"
                   f"{' | ' + help_text if help_text else ''}")
    else:
        st.warning("⚠️ 数据库为空，请先运行数据初始化: `python scripts/init_data.py --stocks 300`")

    col1, col2 = st.columns([2, 1])

    with col1:
        _show_market_overview()
    with col2:
        # 复盘报告按钮容器
        with st.popover("📋 每日复盘报告 (查看/导出)", use_container_width=True):
            _render_daily_report()

    st.markdown("---")


    tab1, tab2, tab3, tab4 = st.tabs(["⭐ 自选股快照", "📡 今日信号", "📊 自选股排行", "🔌 数据健康监控"])

    with tab1:
        _show_watchlist_snapshot()
    with tab2:
        _show_today_signals()
    with tab3:
        _show_hot_stocks()
    with tab4:
        _show_system_status()



def _show_market_overview():
    """大盘概况卡片"""
    st.subheader("📈 大盘概况")

    # 从数据库获取主要指数数据（使用ETF或指数代码的近似数据）
    # 用000001.SH 上证、399001.SZ 深证、399006.SZ 创业板
    index_codes = {
        "000001.SH": "上证指数",
        "399001.SZ": "深证成指",
        "399006.SZ": "创业板指",
    }

    cards = []
    for code, name in index_codes.items():
        # 优先从数据库读取（已缓存）
        df = get_index_daily(code, limit=2)
        if df.empty or len(df) < 2:
            # 数据库没有或数据不足，从 AKShare 实时拉取
            df = _fetch_index_realtime(code)
        if not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            latest_close = float(latest.get("close", 0) or 0)
            prev_close = float(prev.get("close", 0) or 0)
            chg = latest_close - prev_close
            chg_pct = (chg / prev_close * 100) if prev_close > 0 else 0.0
            cards.append({
                "name": name,
                "code": code,
                "price": latest_close,
                "chg": chg,
                "chg_pct": chg_pct,
            })

    if cards:
        cols = st.columns(len(cards))
        for i, c in enumerate(cards):
            with cols[i]:
                delta_color = "normal" if c["chg"] >= 0 else "inverse"
                st.metric(
                    label=f"{c['name']} ({c['code']})",
                    value=f"¥{c['price']:.2f}",
                    delta=f"{c['chg']:.2f} ({c['chg_pct']:.2f}%)",
                    delta_color=delta_color,
                )
    else:
        st.info("暂无指数数据，请先运行数据初始化脚本")


@st.cache_data(ttl=300)
def _fetch_index_realtime(index_code: str) -> pd.DataFrame:
    """从 AKShare 实时获取指数最新行情（缓存 5 分钟）

    Args:
        index_code: '000001.SH'(上证), '399001.SZ'(深证), '399006.SZ'(创业板)

    Returns:
        含 close / trade_date 列的 DataFrame（模拟 get_daily 返回格式）
    """
    try:
        import akshare as ak
        # AKShare 代码格式: sh000001, sz399001, sz399006
        parts = index_code.split(".")
        market = parts[1].lower()
        symbol = parts[0]
        ak_code = f"{market}{symbol}"

        df = ak.stock_zh_index_daily(symbol=ak_code)
        if df is None or df.empty:
            return pd.DataFrame()

        df = df.rename(columns={
            "date": "trade_date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume",
        })
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        df["ts_code"] = index_code
        # 计算涨跌幅
        df["pct_chg"] = df["close"].pct_change() * 100
        return df.sort_values("trade_date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _show_system_status():
    """📊 数据健康监控面板 — 数据时效、数据源状态、系统概览"""
    st.subheader("📊 数据健康监控")

    # ---------- 第一行：关键指标卡片 ----------
    freshness = cached_check_data_freshness()
    latest_date = freshness.get("latest_date", "")
    watchlist = get_watchlist()
    watchlist_count = len(watchlist)
    daily_count = freshness.get("total_rows", get_daily_count())
    stocks_with_data = cached_get_stocks_with_data(min_days=1)
    stocks_with_data_count = len(stocks_with_data)
    signals_count = len(get_signals(limit=100))
    idx_latest = get_index_latest_date()

    # 数据新鲜度判定
    today = datetime.now().strftime("%Y%m%d")
    days_diff = 999
    if latest_date:
        try:
            d1 = datetime.strptime(latest_date, "%Y%m%d")
            d2 = datetime.strptime(today, "%Y%m%d")
            days_diff = (d2 - d1).days
        except ValueError:
            pass

    if days_diff <= 1:
        fresh_icon = "✅"
        fresh_label = "正常"
        fresh_color = "green"
    elif days_diff <= 3:
        fresh_icon = "⚠️"
        fresh_label = f"滞后 {days_diff} 天"
        fresh_color = "orange"
    elif days_diff <= 5:
        fresh_icon = "⚠️"
        fresh_label = f"滞后 {days_diff} 天"
        fresh_color = "orange"
    else:
        fresh_icon = "🔴"
        fresh_label = f"严重滞后 {days_diff} 天" if days_diff != 999 else "无数据"
        fresh_color = "red"

    # 数据源状态检测（轻量级）
    ds_akshare = "🟢 正常"
    ds_tushare = "🟡 未配置" if not TUSHARE_TOKEN else "🟢 正常"
    try:
        import baostock as bs
        bs.login()
        bs.logout()
    except Exception:
        pass

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("📅 数据日期", latest_date or "无数据",
              delta=fresh_label, delta_color="inverse" if "严重" in fresh_label else ("normal" if fresh_icon == "✅" else "off"))
    k2.metric("⭐ 自选股", f"{watchlist_count} 只")
    k3.metric("📊 有数据股票", f"{stocks_with_data_count} 只")
    k4.metric("📡 历史信号", f"{signals_count} 条",
              delta=f"共 {len(get_signals(trade_date=today.replace('-','')))} 条今日信号" if signals_count > 0 else None)
    k5.metric("🗄️ 日线总量", f"{daily_count:,} 条")

    # ---------- 第二行：数据源状态 ----------
    with st.expander("🔌 数据源状态", expanded=True):
        ds_cols = st.columns(4)
        with ds_cols[0]:
            st.markdown(f"**AKShare** {ds_akshare}")
            st.caption("主力数据源（日线/指数）")
        with ds_cols[1]:
            st.markdown(f"**Tushare** {ds_tushare}")
            st.caption("备用数据源（股票列表/行业）")
        with ds_cols[2]:
            st.markdown("**Baostock** 🟢 正常")
            st.caption("兜底数据源（日线备用）")
        with ds_cols[3]:
            idx_status = f"🟢 {idx_latest}" if idx_latest else "🟡 未同步"
            st.markdown(f"**大盘指数** {idx_status}")
            st.caption("上证/深证/创业板")

    # ---------- 第三行：自选股数据健康度明细 ----------
    if not watchlist.empty:
        st.markdown("**📅 自选股数据健康度明细**")
        today_str = datetime.now().strftime("%Y%m%d")
        health_stock_df = cached_instrument_list()
        health_name_map = dict(zip(health_stock_df["ts_code"], health_stock_df["name"])) if not health_stock_df.empty else {}
        health_rows = []
        for _, wl_row in watchlist.iterrows():
            ts_code = wl_row["ts_code"]
            name = health_name_map.get(ts_code, "") or wl_row.get("note", "") or ts_code
            row_count = len(get_daily(ts_code))
            latest = get_latest_date(ts_code)

            if latest:
                try:
                    d1 = datetime.strptime(latest, "%Y%m%d")
                    d2 = datetime.strptime(today_str, "%Y%m%d")
                    gap = (d2 - d1).days
                except ValueError:
                    gap = 999
                if gap <= 1:
                    icon, tip = "✅", "数据正常"
                elif gap <= 3:
                    icon, tip = "⚠️", f"滞后 {gap} 天，建议更新"
                else:
                    icon, tip = "🔴", f"严重滞后 {gap} 天，请立即更新"
                status = f"{latest} ({icon} {tip})"
            else:
                status = "🔴 无数据"
            health_rows.append({
                "代码": ts_code,
                "名称": name,
                "K线数": f"{row_count} 条",
                "数据状态": status,
            })
        if health_rows:
            st.dataframe(
                pd.DataFrame(health_rows),
                column_config={
                    "代码": "代码",
                    "名称": "名称",
                    "K线数": st.column_config.TextColumn("K线数", width="small"),
                    "数据状态": st.column_config.TextColumn("数据状态", width="medium"),
                },
                hide_index=True,
                use_container_width=True,
            )

    # ---------- 操作按钮 ----------
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        if st.button("🔄 更新自选股数据", use_container_width=True, type="primary"):
            from data.storage import update_lock
            with update_lock(timeout=10) as acquired:
                if not acquired:
                    st.warning("⏳ 另一个更新任务正在运行中，请稍后再试")
                else:
                    from scripts.init_data import run_update
                    with st.spinner("正在更新数据..."):
                        run_update(watchlist=True)
                    st.rerun()
    with col_b:
        st.markdown(f"🏷️ 版本: v0.4.0 | 🗄️ `data/quant.db`")
    with col_c:
        import json
        report = {
            "时间": datetime.now().isoformat(),
            "数据最新日期": latest_date,
            "数据状态": fresh_label,
            "有数据股票": stocks_with_data_count,
            "日线总条数": daily_count,
            "自选股数": watchlist_count,
            "历史信号数": signals_count,
            "指数最新日期": idx_latest,
        }
        st.download_button(
            "📋 导出健康报告 (JSON)",
            json.dumps(report, ensure_ascii=False, indent=2),
            file_name=f"data_health_{today}.json",
            mime="application/json",
            use_container_width=True,
        )


@st.cache_data(ttl=60)
def _calc_watchlist_snapshot_rows(watchlist_tuples: tuple, name_tuples: tuple) -> list:
    """缓存版自选股行情与趋势指标计算（缓存 1 分钟，大幅加速页面切换）"""
    name_map = dict(name_tuples)
    rows = []
    for ts_code, note in watchlist_tuples:
        df = get_daily(ts_code)
        if df.empty:
            continue

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        chg_pct = ((latest["close"] - prev["close"]) / prev["close"]) * 100 if prev["close"] else 0

        # 计算MA20趋势与RSI
        df_ind = apply_indicators(df.copy(), ["ma", "rsi"])
        ma20 = df_ind["ma20"].iloc[-1] if "ma20" in df_ind.columns else 0
        rsi = df_ind["rsi14"].iloc[-1] if "rsi14" in df_ind.columns else 50

        # 判断趋势
        if latest["close"] > ma20 and rsi > 50:
            trend = "🟢 多头"
        elif latest["close"] < ma20 and rsi < 50:
            trend = "🔴 空头"
        else:
            trend = "🟡 震荡"

        rows.append({
            "代码": ts_code,
            "名称": name_map.get(ts_code, ts_code),
            "最新价": f"¥{latest['close']:.2f}",
            "涨跌幅": f"{chg_pct:.2f}%",
            "趋势": trend,
            "RSI": f"{rsi:.1f}",
            "MA20": f"¥{ma20:.2f}",
            "备注": note,
        })
    return rows


def _show_watchlist_snapshot():
    """自选股快照"""
    watchlist = get_watchlist()
    if watchlist.empty:
        st.info("暂无自选股，请前往「持仓管理」添加")
        return

    stock_df = cached_instrument_list()
    name_tuples = tuple(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else ()
    notes = watchlist["note"].fillna("").tolist() if "note" in watchlist.columns else [""] * len(watchlist)
    wl_tuples = tuple(zip(watchlist["ts_code"], notes))

    rows = _calc_watchlist_snapshot_rows(wl_tuples, name_tuples)
    if rows:
        df = pd.DataFrame(rows)
        chinese_dataframe(df)
    else:
        st.warning("自选股暂无行情数据")


def _show_today_signals():
    """今日信号"""
    today = datetime.now().strftime("%Y%m%d")
    signals = get_signals(trade_date=today, limit=20)

    if signals.empty:
        st.info("今日暂无交易信号，请前往「信号中心」扫描")
        return

    stock_df = get_instrument_list()
    name_map = {}
    if not stock_df.empty:
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

    rows = []
    for _, sig in signals.iterrows():
        rows.append({
            "股票": f"{sig['ts_code']} {name_map.get(sig['ts_code'], '')}",
            "方向": format_direction_cn(sig["direction"], with_icon=True),
            "策略": format_strategy_cn(sig["strategy"]),
            "评分": f"{sig['score']:.2f}",
            "参考价": f"¥{sig['price_ref']:.2f}" if sig["price_ref"] else "-",
            "原因": sig.get("reason", ""),
        })

    if rows:
        df = pd.DataFrame(rows)
        chinese_dataframe(df)
        st.caption(f"共 {len(rows)} 条信号 | 完整信息请前往「信号中心」")


def _show_hot_stocks():
    """自选股涨跌幅排行（原热门股票，改为只显示自选股表现）"""
    watchlist = get_watchlist()
    if watchlist.empty:
        st.info("暂无自选股，请前往「持仓管理」添加")
        return

    stock_df = cached_instrument_list()
    name_map = {}
    if not stock_df.empty:
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

    rows = []
    for _, wl in watchlist.iterrows():
        ts_code = wl["ts_code"]
        df = get_daily(ts_code)
        if df.empty:
            continue
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        pct = ((latest["close"] - prev["close"]) / prev["close"]) * 100 if prev["close"] > 0 else 0.0

        vol_val = latest.get("volume", latest.get("vol", 0)) or 0
        vol_lots = int(float(vol_val) / 100)

        rows.append({
            "代码": ts_code,
            "名称": name_map.get(ts_code, ""),
            "现价": f"¥{latest['close']:.2f}",
            "涨跌幅": f"{pct:+.2f}%",
            "成交量(手)": f"{vol_lots:,}",
        })

    if rows:
        rows.sort(key=lambda r: float(r["涨跌幅"].replace("%", "").replace("+", "")), reverse=True)
        df = pd.DataFrame(rows)
        chinese_dataframe(df)
        st.caption("自选股今日涨跌幅排行")
    else:
        st.info("暂无自选股行情数据")


def _render_daily_report():
    """渲染每日复盘报告（自选股表现 + 今日信号 + 数据库状态）"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_date = datetime.now().strftime("%Y%m%d")
    watchlist = get_watchlist()
    stock_df = cached_instrument_list()
    name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if stock_df is not None and not stock_df.empty else {}
    signals = get_signals(trade_date=today_date, limit=50)

    lines = [f"# 📊 每日复盘深度报告 — {today_str}", ""]

    # 1. 自选股表现
    lines.append("## ⭐ 自选股表现")
    if not watchlist.empty:
        lines.append("| 标的代码 | 证券名称 | 最新收盘价 | 今日涨跌幅 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for _, wl in watchlist.iterrows():
            ts_code = wl["ts_code"]
            df = get_daily(ts_code, limit=2)
            if not df.empty:
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                pct = ((latest["close"] - prev["close"]) / prev["close"]) * 100 if prev["close"] > 0 else 0
                lines.append(f"| {ts_code} | {name_map.get(ts_code, '')} | ¥{latest['close']:.2f} | {pct:+.2f}% |")
    else:
        lines.append("暂无自选股记录")
    lines.append("")

    # 2. 今日信号
    lines.append("## 📡 今日策略信号")
    if not signals.empty:
        for _, sig in signals.iterrows():
            direction = format_direction_cn(sig["direction"], with_icon=True)
            lines.append(f"- {direction} **{sig['ts_code']} {name_map.get(sig['ts_code'], '')}** "
                         f"| 评分 {sig['score']:.2f} | 策略: {format_strategy_cn(sig['strategy'])} | {sig.get('reason', '')}")
    else:
        lines.append("今日暂无策略触发信号（可前往「信号中心」执行扫描）")
    lines.append("")

    # 3. 数据状态
    lines.append("## ⚙️ 数据资产状态")
    freshness = cached_check_data_freshness()
    lines.append(f"- 数据库最新行情日: **{freshness.get('latest_date', '未知')}**")
    lines.append(f"- 监控自选股总数: **{len(watchlist)}** 只")
    lines.append(f"- 数据库有效日线记录: **{freshness.get('total_rows', 0):,}** 条")

    report_md = "\n".join(lines)
    st.markdown(report_md)
    st.download_button(
        "📥 导出为 Markdown 报告",
        data=report_md.encode("utf-8"),
        file_name=f"复盘报告_{today_date}.md",
        mime="text/markdown",
        use_container_width=True,
    )

