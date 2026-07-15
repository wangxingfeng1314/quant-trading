"""策略介绍页面 - 展示所有策略的说明、参数、回测表现和排行榜"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
from strategies import STRATEGY_REGISTRY, list_strategies
from app.st_utils import chinese_dataframe
from data.storage import get_conn


def _get_strategy_stats() -> dict:
    """从回测记录中统计各策略的历史表现"""
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

    # 概览统计
    has_backtest = sum(1 for s in strategies if strategy_stats.get(s["name"]))
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("策略总数", total)
    col2.metric("趋势跟踪", sum(1 for s in strategies if "趋势" in s["desc"] or "突破" in s["desc"]))
    col3.metric("反转交易", sum(1 for s in strategies if "反转" in s["desc"] or "背离" in s["desc"]))
    col4.metric("有回测数据", has_backtest)

    # === 策略排行榜 ===
    st.markdown("---")
    st.subheader("🏆 策略排行榜（基于历史回测）")

    ranking_data = []
    for s in strategies:
        stats = strategy_stats.get(s["name"])
        if stats:
            ranking_data.append({
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
                [["策略", "平均总收益%", "平均年化%", "平均夏普", "平均胜率%", "回测次数"]]
            )
        with col_tab2:
            st.markdown("**按夏普比率排序**")
            chinese_dataframe(
                df_rank.sort_values("平均夏普", ascending=False)
                [["策略", "平均夏普", "平均总收益%", "平均最大回撤%", "平均胜率%", "回测次数"]]
            )
    else:
        st.info("暂无回测数据，请先在回测中心运行回测")

    # === 每个策略详情卡片 ===
    st.markdown("---")
    st.subheader("📖 策略详情")

    for s in strategies:
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
            }
            usage = usage_map.get(s["name"], "")
            if usage:
                st.markdown(f"**💡 使用建议**：{usage}")
