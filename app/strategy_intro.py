"""策略介绍页面 - 展示所有策略的说明、参数、回测表现和排行榜"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
from strategies import STRATEGY_REGISTRY, list_strategies
from app.st_utils import chinese_dataframe, strategy_style_badge
from data.storage import get_conn


@st.cache_data(ttl=180)
def _get_strategy_stats() -> dict:
    """从回测记录中统计各策略的历史表现（缓存 3 分钟）"""
    stats = {}
    with get_conn() as conn:
        for s in list_strategies():
            name = s["name"]
            df = pd.read_sql(
                "SELECT total_return, sharpe_ratio, max_drawdown, win_rate, "
                "trade_count, annual_return FROM backtest_result WHERE strategy = ?",
                conn, params=[name]
            )
            if df.empty:
                stats[name] = None
            else:
                stats[name] = {
                    "回测次数": len(df),
                    "平均总收益%": round(df["total_return"].mean(), 2),
                    "平均年化%": round(df["annual_return"].mean(), 2),
                    "平均夏普": round(df["sharpe_ratio"].mean(), 2),
                    "平均最大回撤%": round(df["max_drawdown"].mean(), 2),
                    "平均胜率%": round(df["win_rate"].mean(), 1),
                    "总交易次数": int(df["trade_count"].sum()),
                }
    return stats


def show():
    st.title("📚 策略百科")
    st.caption("了解每个策略的原理、参数含义、历史回测表现")

    strategies = list_strategies()
    total = len(strategies)
    strategy_stats = _get_strategy_stats()

    # 概览统计（按策略风格分类）
    has_backtest = sum(1 for s in strategies if strategy_stats.get(s["name"]))
    style_count = {}
    for s in strategies:
        style = s.get("style", "综合")
        style_count[style] = style_count.get(style, 0) + 1
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("策略总数", total)
    col2.metric("⚡ 短线", style_count.get("短线", 0))
    col3.metric("↔️ 震荡", style_count.get("震荡", 0))
    col4.metric("📈 中长线", style_count.get("中长线", 0))
    col5.metric("🧩 综合", style_count.get("综合", 0))

    # === 策略排行榜 ===
    st.markdown("---")
    st.subheader("🏆 策略排行榜（基于历史回测）")

    ranking_data = []
    for s in strategies:
        stats = strategy_stats.get(s["name"])
        if stats:
            ranking_data.append({
                "风格": s.get("style", "综合"),
                "策略": s["name"],
                "说明": s["desc"],
                "平均总收益%": stats["平均总收益%"],
                "平均年化%": stats["平均年化%"],
                "平均夏普": stats["平均夏普"],
                "平均最大回撤%": stats["平均最大回撤%"],
                "平均胜率%": stats["平均胜率%"],
                "回测次数": stats["回测次数"],
                "总交易次数": stats["总交易次数"],
            })

    if ranking_data:
        df_rank = pd.DataFrame(ranking_data)
        col_tab1, col_tab2 = st.columns(2)
        with col_tab1:
            st.markdown("**按总收益排序**")
            chinese_dataframe(
                df_rank.sort_values("平均总收益%", ascending=False)
                [["风格", "策略", "平均总收益%", "平均年化%", "平均夏普", "平均胜率%", "回测次数"]]
            )
        with col_tab2:
            st.markdown("**按夏普比率排序**")
            chinese_dataframe(
                df_rank.sort_values("平均夏普", ascending=False)
                [["风格", "策略", "平均夏普", "平均总收益%", "平均最大回撤%", "平均胜率%", "回测次数"]]
            )
    else:
        st.info("暂无回测数据，请先在回测中心运行回测")

    # === 每个策略详情卡片（按风格分组展示） ===
    st.markdown("---")
    st.subheader("📖 策略详情")

    # 按风格分组：短线 → 震荡 → 中长线 → 综合
    STYLE_ORDER = ["短线", "震荡", "中长线", "综合"]
    grouped: dict = {style: [] for style in STYLE_ORDER}
    for s in strategies:
        grouped.setdefault(s.get("style", "综合"), []).append(s)

    for style in STYLE_ORDER:
        style_list = grouped.get(style, [])
        if not style_list:
            continue
        st.markdown(f"#### {strategy_style_badge(style)}（{len(style_list)}个）",
                    unsafe_allow_html=True)
        for s in style_list:
            cls = STRATEGY_REGISTRY[s["name"]]
            doc = cls.__doc__ or ""
            stats = strategy_stats.get(s["name"])

            label = f"**{s['name']}** — {s['desc']}"
            if stats:
                label += f"  (📈 平均收益 {stats['平均总收益%']:+.1f}%)"

            with st.expander(label, expanded=False):
                col_left, col_right = st.columns([3, 2])

                with col_left:
                    lines = doc.strip().split("\n")
                    desc_lines = [l.strip() for l in lines if l.strip() and l.strip() not in ('"""', "'''")]
                    for l in desc_lines:
                        st.markdown(l)

                    # 回测表现
                    if stats:
                        st.markdown("**📊 历史回测表现**")
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("平均收益", f"{stats['平均总收益%']:+.2f}%")
                        m2.metric("平均夏普", f"{stats['平均夏普']:.2f}")
                        m3.metric("平均胜率", f"{stats['平均胜率%']:.1f}%")
                        m4.metric("回测次数", stats['回测次数'])

                with col_right:
                    if s["params"]:
                        st.markdown("**⚙️ 参数说明**")
                        param_df = []
                        for pname, pinfo in s["params"].items():
                            param_df.append({
                                "参数名": pname,
                                "默认值": pinfo["default"],
                                "说明": pinfo["desc"],
                            })
                        chinese_dataframe(pd.DataFrame(param_df), height=200)
                    else:
                        st.info("该策略无可调参数")

                usage_map = {
                    "ma_cross": "适合中长线波段操作，在趋势明显的股票上效果较好。",
                    "macd_divergence": "适合捕捉趋势反转点，震荡行情中表现优异。",
                    "turtle": "适合强趋势行情，能捕捉大波段利润。",
                    "rsi_oversold": "适合震荡行情中的反转交易，超卖超买区域有效。",
                    "bollinger_reversal": "适合震荡行情波段操作，价格回归均值时触发。",
                    "kdj_cross": "适合短线交易，灵敏度高，注意假信号。",
                    "ma_bullish": "适合中长线趋势跟踪，多头排列确立后入场。",
                    "donchian_breakout": "适合强趋势行情，与海龟策略互补，通道突破+ATR止损体系更完整。",
                    "volume_price_breakout": "适合趋势启动初期，放量突破确认比单纯价格突破更可靠，减少假突破。",
                    "double_bottom": "适合震荡筑底阶段的股票，W底形态是经典反转信号，胜率较高。",
                    "multi_factor": "适合作为参考策略，综合多个因子视角，不单独作为交易决策依据。",
                    "macd_cross": "短线灵敏，快进快出；低位金叉（DIF<0）信号更可靠。",
                    "ma_pullback": "适合短线强势股，回调不破均线企稳时介入，止损参考跌破均线。",
                    "rsi_divergence": "与MACD背离互补，震荡市底部反转信号，建议配合量能确认。",
                    "boll_squeeze": "捕捉震荡末期变盘方向，带宽压缩越久突破越有力，需放量确认。",
                    "ma60_breakout": "中长线趋势转折信号，放量突破+均线走平向上同时满足时更可靠。",
                    "signal_combo": "综合过滤信号，多维度共振时胜率更高，建议作为买入前最后一道确认。",
                }
                usage = usage_map.get(s["name"], "")
                if usage:
                    st.markdown(f"**💡 使用建议**：{usage}")
