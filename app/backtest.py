"""回测中心页面 - 运行回测 + 参数优化 + 多策略对比 + 历史记录"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import numpy as np
import json
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date

from app.st_utils import chinese_dataframe, chinese_date_picker
from strategies import STRATEGY_REGISTRY, list_strategies
from engine.backtester import Backtester, grid_search
from data.storage import get_stock_list, get_daily, get_backtest_results, get_backtest_trades, get_stocks_with_data, get_watchlist
from data.indicators import apply_indicators
from core.config import DEFAULT_CAPITAL


def show():
    st.title("🔬 回测中心")

    tab1, tab2, tab3, tab4 = st.tabs(["运行回测", "参数优化", "多策略对比", "历史记录"])

    with tab1:
        _show_run_backtest()
    with tab2:
        _show_grid_search()
    with tab3:
        _show_multi_strategy()
    with tab4:
        _show_history()


def _show_run_backtest():
    """运行回测（单次）"""
    stock_df = get_stock_list()
    stocks_with_data = get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无股票数据，请先运行初始化脚本")
        return

    # 过滤：只保留有数据的股票（避免用户选到无数据的股票）
    stock_df_data = stock_df[stock_df["ts_code"].isin(stocks_with_data)].copy()
    if stock_df_data.empty:
        st.warning("暂无足够行情数据的股票（需至少60条日线），请先下载数据")
        return

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("策略设置")
        strategies = list_strategies()
        strategy_names = [s["name"] for s in strategies]
        strategy_descs = {s["name"]: s["desc"] for s in strategies}

        selected_strategy = st.selectbox(
            "选择策略",
            strategy_names,
            format_func=lambda x: f"{x} - {strategy_descs[x]}",
            key="bt_strategy"
        )

        strategy_cls = STRATEGY_REGISTRY[selected_strategy]
        params = {}
        if strategy_cls.param_schema:
            st.markdown("**策略参数**")

            # 参数模板保存/加载
            import json
            from pathlib import Path
            template_dir = Path(__file__).parent.parent / "data" / "templates"
            template_dir.mkdir(exist_ok=True)
            template_file = template_dir / f"{selected_strategy}_templates.json"
            templates = json.loads(template_file.read_text(encoding="utf-8")) \
                if template_file.exists() else {}

            col_t1, col_t2 = st.columns([3, 1])
            with col_t1:
                saved_names = list(templates.keys())
                load_options = ["", "📌 当前参数"]
                load_options += [n for n in saved_names if n != "__current__"]
                load_name = st.selectbox(
                    "加载已保存的参数模板",
                    load_options,
                    key="bt_load_template",
                )
                # 映射"当前参数"到特殊键
                if load_name == "📌 当前参数":
                    load_name = "__current__"
            with col_t2:
                st.markdown("")  # 占位
                if saved_names and st.button("🗑️ 删除", key="bt_del_template"):
                    if load_name and load_name in templates:
                        templates.pop(load_name)
                        template_file.write_text(json.dumps(templates, ensure_ascii=False, indent=2))
                        st.rerun()

            # 加载模板参数
            default_params = templates.get(load_name, {}) if load_name else {}

            for pname, pinfo in strategy_cls.param_schema.items():
                default = pinfo.get("default", 0)
                # 使用模板值覆盖默认值
                if load_name and pname in default_params:
                    default = default_params[pname]
                desc = pinfo.get("desc", pname)
                if isinstance(default, int):
                    params[pname] = st.number_input(
                        desc, value=default, min_value=1, key=f"bt_param_{pname}"
                    )
                elif isinstance(default, float):
                    params[pname] = st.number_input(
                        desc, value=default, key=f"bt_param_{pname}"
                    )
                else:
                    params[pname] = st.text_input(desc, value=str(default), key=f"bt_param_{pname}")

            # 保存模板按钮
            save_name = st.text_input("保存为参数模板", placeholder="如: 趋势跟踪-激进",
                                       key="bt_save_name", label_visibility="collapsed")
            if save_name and st.button("💾 保存模板", key="bt_save_template"):
                templates[save_name] = params
                template_file.write_text(json.dumps(templates, ensure_ascii=False, indent=2))
                st.success(f"✅ 已保存模板: {save_name}")
                st.rerun()

            # 标记为当前参数
            if st.button("📌 标记为当前策略参数", key="bt_mark_current", type="secondary"):
                templates["__current__"] = params
                template_file.write_text(json.dumps(templates, ensure_ascii=False, indent=2))
                st.success("✅ 已标记为当前参数")
                st.rerun()

    with col2:
        st.subheader("回测参数")

        mode = st.radio("股票范围", ["单只股票", "多只股票(手动)", "全部自选股"], horizontal=True, key="bt_mode")

        if mode == "单只股票":
            search = st.text_input("搜索股票", placeholder="代码或名称", key="bt_search")
            if search:
                mask = (stock_df_data["ts_code"].str.contains(search, case=False) |
                        stock_df_data["name"].str.contains(search, case=False))
                filtered = stock_df_data[mask]
            else:
                filtered = stock_df_data.head(50)

            options = filtered.apply(
                lambda r: f"{r['ts_code']} {r['name']}", axis=1
            ).tolist()

            if options:
                selected = st.selectbox("选择股票", options, key="bt_stock")
                ts_code = selected.split(" ")[0]
                universe = [ts_code]
            else:
                st.warning(f"未找到匹配股票（当前仅有 {len(stock_df_data)} 只股票有数据）")
                return
        elif mode == "全部自选股":
            watchlist = get_watchlist()
            universe = watchlist["ts_code"].tolist() if not watchlist.empty else []
            if not universe:
                st.warning("自选股列表为空，请先添加自选股")
                return
            st.caption(f"📊 将对 {len(universe)} 只自选股逐只回测并对比结果")
        else:
            codes_input = st.text_area(
                "输入股票代码（每行一个）",
                placeholder="000001.SZ\n600519.SH\n000858.SZ",
                height=100, key="bt_codes"
            )
            universe = [c.strip() for c in codes_input.strip().split("\n") if c.strip()]

        date_col1, date_col2 = st.columns(2)
        with date_col1:
            start_date = chinese_date_picker("开始日期", default_val=date(2023,1,1), key="bt_start")
        with date_col2:
            end_date = chinese_date_picker("结束日期", default_val=date.today(), key="bt_end")

        capital = st.number_input(
            "初始资金(元)", value=int(DEFAULT_CAPITAL),
            min_value=10000, step=10000, key="bt_capital"
        )

        # 基准对比选项
        show_benchmark = st.checkbox("叠加沪深300基准对比", value=True, key="bt_benchmark")

    if st.button("🚀 运行回测", type="primary", width='stretch'):
        if not universe:
            st.error("请选择至少一只股票")
            return

        # 批量回测自选股模式
        if mode == "全部自选股":
            results = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            for i, ts_code in enumerate(universe):
                status_text.text(f"回测中 {i+1}/{len(universe)}: {ts_code}")
                bt = Backtester(
                    strategy_cls=strategy_cls, params=params,
                    universe=[ts_code],
                    start_date=start_date, end_date=end_date,
                    initial_capital=float(capital),
                )
                result = bt.run(save=False)
                if result.equity_curve:
                    results.append({"ts_code": ts_code, "result": result})
                progress_bar.progress((i + 1) / len(universe))

            progress_bar.empty()
            status_text.empty()

            if not results:
                st.warning("所有自选股均无交易产生")
                return

            st.success(f"回测完成！{len(results)}/{len(universe)} 只自选股产生交易")

            # 对比表格
            stock_df = get_stock_list()
            name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))
            compare_rows = []
            for r in results:
                res = r["result"]
                compare_rows.append({
                    "股票": f"{r['ts_code']} {name_map.get(r['ts_code'], '')}",
                    "总收益%": f"{res.total_return:+.2f}",
                    "年化%": f"{res.annual_return:+.2f}",
                    "夏普": f"{res.sharpe_ratio:.2f}",
                    "最大回撤%": f"{res.max_drawdown:.2f}",
                    "胜率%": f"{res.win_rate:.1f}",
                    "交易次数": res.trade_count,
                })

            df_compare = pd.DataFrame(compare_rows)
            st.subheader("📊 自选股回测对比")
            st.dataframe(
                df_compare.style.highlight_max(subset=["总收益%", "夏普", "胜率%"],
                                                color="#90EE90"),
                hide_index=True, use_container_width=True,
            )

            # 最佳结果展示
            best = max(results, key=lambda r: r["result"].total_return)
            st.subheader(f"🏆 最佳表现: {best['ts_code']} {name_map.get(best['ts_code'], '')}")
            st.caption(f"总收益 {best['result'].total_return:+.2f}% | "
                       f"夏普 {best['result'].sharpe_ratio:.2f} | "
                       f"胜率 {best['result'].win_rate:.1f}%")
            return

        # 单只/多只模式
        with st.spinner("正在回测中..."):
            bt = Backtester(
                strategy_cls=strategy_cls,
                params=params,
                universe=universe,
                start_date=start_date, end_date=end_date,
                initial_capital=float(capital),
            )
            result = bt.run(save=True)

        if not result.equity_curve:
            st.warning("回测无交易产生，请调整参数或日期范围")
            return

        benchmark_curve = None
        if show_benchmark:
            benchmark_curve = _get_benchmark_curve(start_date, end_date)

        _display_result(result, benchmark_curve=benchmark_curve)


def _show_grid_search():
    """参数优化（网格搜索）"""
    stock_df = get_stock_list()
    stocks_with_data = get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无股票数据")
        return

    # 只保留有数据的股票
    stock_df_data = stock_df[stock_df["ts_code"].isin(stocks_with_data)].copy()
    if stock_df_data.empty:
        st.warning("暂无足够行情数据的股票（需至少60条日线），请先下载数据")
        return

    st.subheader("⚙️ 参数优化")
    st.caption("自动遍历参数组合，找到最优参数配置")

    col1, col2 = st.columns(2)

    with col1:
        strategies = list_strategies()
        strategy_names = [s["name"] for s in strategies]
        strategy_descs = {s["name"]: s["desc"] for s in strategies}

        selected_strategy = st.selectbox(
            "选择策略",
            strategy_names,
            format_func=lambda x: f"{x} - {strategy_descs[x]}",
            key="gs_strategy"
        )
        strategy_cls = STRATEGY_REGISTRY[selected_strategy]

    with col2:
        metric = st.selectbox(
            "优化目标",
            ["total_return", "sharpe_ratio", "win_rate", "max_drawdown"],
            format_func=lambda x: {
                "total_return": "总收益率 ↑",
                "sharpe_ratio": "夏普比率 ↑",
                "win_rate": "胜率 ↑",
                "max_drawdown": "最大回撤 ↓",
            }.get(x, x),
            key="gs_metric"
        )

    # 参数网格设置
    st.markdown("**参数搜索范围**")
    if not strategy_cls.param_schema:
        st.info("该策略无可调参数")
        return

    param_grid = {}
    cols = st.columns(len(strategy_cls.param_schema))
    for i, (pname, pinfo) in enumerate(strategy_cls.param_schema.items()):
        with cols[i]:
            default = pinfo.get("default", 10)
            st.markdown(f"**{pinfo.get('desc', pname)}**")

            val_type = "int" if isinstance(default, int) else "float"
            min_v = st.number_input("最小值", value=int(default * 0.5) if val_type == "int" else default * 0.5,
                                     key=f"gs_min_{pname}")
            max_v = st.number_input("最大值", value=int(default * 2) if val_type == "int" else default * 2,
                                     key=f"gs_max_{pname}")
            steps = st.number_input("步数", value=5, min_value=2, max_value=20, key=f"gs_steps_{pname}")

            if val_type == "int":
                param_grid[pname] = list(range(int(min_v), int(max_v) + 1, max(1, int((max_v - min_v) / (steps - 1)))))
            else:
                param_grid[pname] = [round(min_v + i * (max_v - min_v) / (steps - 1), 1) for i in range(steps)]

    # 股票选择
    mode = st.radio("股票", ["单只股票", "多只股票(手动)"], horizontal=True, key="gs_mode")
    if mode == "单只股票":
        options = stock_df_data.apply(lambda r: f"{r['ts_code']} {r['name']}", axis=1).head(100).tolist()
        selected = st.selectbox("选择股票", options, key="gs_stock")
        universe = [selected.split(" ")[0]]
    else:
        codes_input = st.text_area("股票代码（每行一个）", height=80, key="gs_codes",
                                    placeholder="000001.SZ\n600519.SH")
        universe = [c.strip() for c in codes_input.strip().split("\n") if c.strip()]

    date_col1, date_col2 = st.columns(2)
    with date_col1:
        gs_start = chinese_date_picker("开始日期", default_val=date(2023,1,1), key="gs_start")
    with date_col2:
        gs_end = chinese_date_picker("结束日期", default_val=date.today(), key="gs_end")

    # 总组合数
    total_combos = 1
    for v in param_grid.values():
        total_combos *= len(v)
    st.caption(f"参数组合数: {total_combos}")

    if st.button("🚀 开始参数优化", type="primary", width='stretch'):
        if not universe or not param_grid:
            st.error("请设置参数范围和股票")
            return

        progress_bar = st.progress(0)
        status_text = st.empty()

        def on_progress(completed, total):
            progress_bar.progress(completed / total)
            status_text.text(f"已完成 {completed}/{total}")

        results = grid_search(
            strategy_cls=strategy_cls,
            universe=universe,
            start_date=gs_start, end_date=gs_end,
            initial_capital=DEFAULT_CAPITAL,
            param_grid=param_grid,
            metric=metric.replace("-", ""),
            progress_callback=on_progress,
        )

        progress_bar.empty()
        status_text.empty()

        if not results:
            st.warning("所有组合均无交易产生")
            return

        st.success(f"参数优化完成！共测试 {len(results)} 种参数组合")

        # 显示最佳结果
        best = results[0]
        st.subheader("🏆 最优参数")
        best_params_str = ", ".join(f"{k}={v}" for k, v in best["params"].items())
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("总收益", f"{best['result'].total_return:.2f}%")
        c2.metric("夏普比率", f"{best['result'].sharpe_ratio:.2f}")
        c3.metric("最大回撤", f"{best['result'].max_drawdown:.2f}%")
        c4.metric("胜率", f"{best['result'].win_rate:.1f}%")
        c5.metric("最优参数", best_params_str)

        # 热力图（2D参数时）
        if len(param_grid) == 2:
            _draw_heatmap(results, param_grid, metric)

        # 结果表格
        st.subheader("📋 所有参数组合")
        table_rows = []
        for r in results:
            row = {k: v for k, v in r["params"].items()}
            row.update({
                "总收益%": f"{r['result'].total_return:.2f}",
                "夏普": f"{r['result'].sharpe_ratio:.2f}",
                "最大回撤%": f"{r['result'].max_drawdown:.2f}",
                "胜率%": f"{r['result'].win_rate:.1f}",
                "交易次数": r['result'].trade_count,
            })
            table_rows.append(row)
        chinese_dataframe(pd.DataFrame(table_rows))


def _draw_heatmap(results, param_grid, metric):
    """绘制2D参数热力图"""
    param_names = list(param_grid.keys())
    x_values = sorted(param_grid[param_names[1]])
    y_values = sorted(param_grid[param_names[0]])

    heat_data = np.full((len(y_values), len(x_values)), np.nan)
    for r in results:
        yi = y_values.index(r["params"][param_names[0]])
        xi = x_values.index(r["params"][param_names[1]])
        heat_data[yi][xi] = r["metric_value"]

    metric_label = {
        "total_return": "总收益率 (%)",
        "sharpe_ratio": "夏普比率",
        "win_rate": "胜率 (%)",
        "max_drawdown": "最大回撤 (%)",
    }.get(metric, metric)

    fig = go.Figure(data=go.Heatmap(
        z=heat_data,
        x=x_values,
        y=y_values,
        colorscale="RdYlGn",
        text=np.round(heat_data, 2),
        texttemplate="%{text}",
        hovertemplate=f"{param_names[0]}: %{{y}}<br>{param_names[1]}: %{{x}}<br>{metric_label}: %{{z:.2f}}<extra></extra>",
    ))
    fig.update_layout(
        title=f"参数热力图 - {metric_label}",
        xaxis_title=param_names[1],
        yaxis_title=param_names[0],
        template="plotly_dark",
        height=500,
    )
    st.plotly_chart(fig, width='stretch')


def _show_multi_strategy():
    """多策略对比"""
    stock_df = get_stock_list()
    stocks_with_data = get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无股票数据")
        return

    st.subheader("📊 多策略权益曲线对比")

    strategies = list_strategies()
    strategy_names = [s["name"] for s in strategies]
    strategy_descs = {s["name"]: s["desc"] for s in strategies}

    selected_strategies = st.multiselect(
        "选择要对比的策略",
        strategy_names,
        default=strategy_names,
        format_func=lambda x: f"{x} - {strategy_descs[x]}",
    )

    if not selected_strategies:
        st.warning("请至少选择一个策略")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        codes_input = st.text_input("股票代码", placeholder="000001.SZ", key="ms_code")
        st.caption(f"📊 有数据的股票: {len(stocks_with_data)} 只")
    with col2:
        ms_start = chinese_date_picker("开始日期", default_val=date(2023,1,1), key="ms_start")
    with col3:
        ms_end = chinese_date_picker("结束日期", default_val=date.today(), key="ms_end")

    show_benchmark = st.checkbox("叠加沪深300基准", value=True, key="ms_benchmark")
    matrix_mode = st.checkbox("📊 矩阵热力图模式（策略×自选股）", value=False, key="ms_matrix",
                              help="对所有选中的策略和自选股逐对回测，用热力图展示总收益")

    if st.button("🚀 运行对比", type="primary", width='stretch'):
        # 矩阵热力图模式：策略 × 自选股
        if matrix_mode:
            watchlist = get_watchlist()
            watchlist_codes = watchlist["ts_code"].tolist() if not watchlist.empty else []
            if not watchlist_codes:
                st.warning("自选股列表为空，请先添加自选股")
                return

            stock_df = get_stock_list()
            name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

            # 构建矩阵数据
            matrix_data = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            total_runs = len(selected_strategies) * len(watchlist_codes)
            run_count = 0

            for sname in selected_strategies:
                scls = STRATEGY_REGISTRY[sname]
                params = {}
                if scls.param_schema:
                    for pname, pinfo in scls.param_schema.items():
                        params[pname] = pinfo["default"]

                for ts_code in watchlist_codes:
                    run_count += 1
                    status_text.text(f"回测中 {run_count}/{total_runs}: {sname} × {ts_code}")
                    bt = Backtester(
                        strategy_cls=scls, params=params,
                        universe=[ts_code],
                        start_date=ms_start, end_date=ms_end,
                        initial_capital=DEFAULT_CAPITAL,
                    )
                    result = bt.run(save=False)
                    ret = result.total_return if result.equity_curve else None
                    matrix_data.append({
                        "strategy": sname,
                        "stock": ts_code,
                        "stock_name": name_map.get(ts_code, ts_code),
                        "return": ret,
                    })
                    progress_bar.progress(run_count / total_runs)

            progress_bar.empty()
            status_text.empty()

            # 过滤有数据的组合
            matrix_data = [d for d in matrix_data if d["return"] is not None]
            if not matrix_data:
                st.warning("所有组合均无交易产生")
                return

            df_matrix = pd.DataFrame(matrix_data)
            st.success(f"矩阵回测完成！{len(df_matrix)} 个有效组合")

            # 热力图
            pivot = df_matrix.pivot_table(
                index="strategy", columns="stock_name", values="return", aggfunc="mean"
            )
            fig = go.Figure(data=go.Heatmap(
                z=pivot.values,
                x=pivot.columns.tolist(),
                y=pivot.index.tolist(),
                colorscale="RdYlGn",
                text=np.round(pivot.values, 1),
                texttemplate="%{text}%",
                hovertemplate="策略: %{y}<br>股票: %{x}<br>收益: %{z:.1f}%<extra></extra>",
            ))
            fig.update_layout(
                title="📊 策略×股票 回测收益热力图",
                template="plotly_dark", height=400,
                xaxis_title="股票", yaxis_title="策略",
            )
            st.plotly_chart(fig, width='stretch')

            # 最佳组合
            best = max(matrix_data, key=lambda d: d["return"])
            st.subheader(f"🏆 最佳组合: {best['strategy']} × {best['stock_name']}")
            st.caption(f"总收益 {best['return']:+.2f}%")
            return

        # 原有单只股票对比模式
        if not codes_input:
            st.error("请输入股票代码")
            return

        universe = [codes_input.strip()]
        start_str = ms_start
        end_str = ms_end

        curves = []
        names = []

        with st.spinner("正在运行多策略回测..."):
            for sname in selected_strategies:
                scls = STRATEGY_REGISTRY[sname]
                params = {}
                if scls.param_schema:
                    for pname, pinfo in scls.param_schema.items():
                        params[pname] = pinfo["default"]

                bt = Backtester(
                    strategy_cls=scls,
                    params=params,
                    universe=universe,
                    start_date=start_str,
                    end_date=end_str,
                    initial_capital=DEFAULT_CAPITAL,
                )
                result = bt.run(save=False)
                if result.equity_curve:
                    curves.append(result.equity_curve)
                    names.append(sname)

        if not curves:
            st.warning("所有策略均无交易产生")
            return

        # 绘制叠加图
        fig = go.Figure()

        for curve, name in zip(curves, names):
            df_eq = pd.DataFrame(curve)
            total_ret = (df_eq["equity"].iloc[-1] / df_eq["equity"].iloc[0] - 1) * 100
            fig.add_trace(go.Scatter(
                x=df_eq["date"], y=df_eq["equity"],
                mode="lines", name=f"{name} ({total_ret:+.1f}%)",
                line=dict(width=2),
            ))

        # 叠加沪深300
        if show_benchmark:
            benchmark_curve = _get_benchmark_curve(start_str, end_str)
            if benchmark_curve:
                bm_df = pd.DataFrame(benchmark_curve)
                fig.add_trace(go.Scatter(
                    x=bm_df["date"], y=bm_df["equity"],
                    mode="lines", name="沪深300 (基准)",
                    line=dict(width=2, dash="dash", color="gray"),
                ))

        fig.update_layout(
            title="多策略权益曲线对比",
            template="plotly_dark",
            height=500,
            xaxis_title="日期", yaxis_title="权益(元)",
            xaxis=dict(type="category", nticks=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, width='stretch')

        # 指标对比表
        st.subheader("📋 指标对比")
        rows = []
        for curve, name in zip(curves, names):
            df_eq = pd.DataFrame(curve)
            initial = df_eq["equity"].iloc[0]
            final = df_eq["equity"].iloc[-1]
            total_ret = (final / initial - 1) * 100
            days = len(df_eq)
            annual_ret = ((final / initial) ** (252 / max(days, 1)) - 1) * 100

            # 最大回撤
            peak = df_eq["equity"].cummax()
            dd = ((peak - df_eq["equity"]) / peak * 100).max()

            rows.append({
                "策略": name,
                "总收益%": f"{total_ret:.2f}",
                "年化收益%": f"{annual_ret:.2f}",
                "最大回撤%": f"{dd:.2f}",
                "交易次数": len([c for c in curves if c is not None]),
            })

            chinese_dataframe(pd.DataFrame(rows))


def _show_nlp_summary(result):
    """回测结果的自然语言结论（让非量化用户也能看懂）"""
    lines = []
    verdict = ""

    # ---------- 样本量判断 ----------
    if result.trade_count < 5:
        lines.append(
            f"🔴 **样本不足**：仅 {result.trade_count} 次交易，统计意义有限，结论仅供参考")
    elif result.trade_count < 15:
        lines.append(
            f"🟡 **样本偏少**：{result.trade_count} 次交易，建议继续积累数据")

    # ---------- 总收益/年化 ----------
    if result.annual_return > 20:
        lines.append(f"✅ **年化收益 {result.annual_return:.1f}%** 表现优秀，远超理财产品")
        verdict = "positive"
    elif result.annual_return > 10:
        lines.append(f"✅ **年化收益 {result.annual_return:.1f}%** 表现良好")
        verdict = "positive"
    elif result.annual_return > 5:
        lines.append(f"🟡 **年化收益 {result.annual_return:.1f}%** 一般，略高于理财")
    elif result.annual_return > 0:
        lines.append(f"⚪ **年化收益 {result.annual_return:.1f}%** 勉强跑赢，建议优化参数")
    else:
        lines.append(f"🔴 **年化收益 {result.annual_return:.1f}%** 亏损状态，建议修改策略参数")
        verdict = "negative"

    # ---------- 回撤 ----------
    if result.max_drawdown > 30:
        lines.append(f"🔴 **回撤风险极高**：最大回撤 {result.max_drawdown:.1f}%，建议加止损")
    elif result.max_drawdown > 20:
        lines.append(f"🟡 **回撤偏高**：最大回撤 {result.max_drawdown:.1f}%，需注意风险")
    elif result.max_drawdown > 10:
        lines.append(f"⚪ **回撤适中**：最大回撤 {result.max_drawdown:.1f}%，可接受")
    else:
        lines.append(f"✅ **回撤控制优秀**：最大回撤仅 {result.max_drawdown:.1f}%")

    # ---------- 夏普比率 ----------
    if result.sharpe_ratio > 2:
        lines.append(f"✅ **夏普 {result.sharpe_ratio:.2f}** 极高，每单位风险回报优秀")
    elif result.sharpe_ratio > 1.5:
        lines.append(f"✅ **夏普 {result.sharpe_ratio:.2f}** 较高，风险调整后收益好")
    elif result.sharpe_ratio > 1:
        lines.append(f"🟡 **夏普 {result.sharpe_ratio:.2f}** 合格，高于无风险利率")
    elif result.sharpe_ratio > 0:
        lines.append(f"⚪ **夏普 {result.sharpe_ratio:.2f}** 偏低，风险调整后收益不理想")
    else:
        lines.append(f"🔴 **夏普 {result.sharpe_ratio:.2f}** 为负，风险调整后亏损")

    # ---------- 胜率 ----------
    if result.trade_count >= 5:
        if result.win_rate > 60:
            lines.append(f"✅ **胜率 {result.win_rate:.0f}%** 较高，策略信号准确")
        elif result.win_rate > 40:
            lines.append(f"⚪ **胜率 {result.win_rate:.0f}%** 中等")
        else:
            lines.append(f"🟡 **胜率 {result.win_rate:.0f}%** 偏低，靠大赚小亏策略")
            verdict = "neutral"

    # ---------- 卡玛比率 ----------
    if result.calmar_ratio > 3:
        lines.append(f"✅ **卡玛比 {result.calmar_ratio:.2f}** 优秀，收益/回撤性价比高")
    elif result.calmar_ratio > 1:
        lines.append(f"⚪ **卡玛比 {result.calmar_ratio:.2f}** 一般")
    elif result.calmar_ratio > 0:
        lines.append(f"🔴 **卡玛比 {result.calmar_ratio:.2f}** 偏低，回撤相对收益过大")

    # ---------- 最终整体结论 ----------
    if verdict == "positive":
        conclusion = "🟢 **综合评估：值得关注** — 该策略整体表现良好，可以考虑实盘验证"
    elif verdict == "negative":
        conclusion = "🔴 **综合评估：不建议使用** — 建议修改策略参数或换用其他策略"
    else:
        conclusion = "🟡 **综合评估：需谨慎** — 策略有亮点也有风险，建议观察更多数据"

    lines.append(f"\n💡 **结论：** {conclusion}")

    st.info("\n\n".join(lines))


def _display_result(result, benchmark_curve=None):
    """展示回测结果（含绩效归因）"""
    st.subheader("📊 回测结果")

    # 指标卡片
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总收益", f"{result.total_return:.2f}%")
    c2.metric("年化收益", f"{result.annual_return:.2f}%")
    c3.metric("最大回撤", f"{result.max_drawdown:.2f}%")
    c4.metric("夏普比率", f"{result.sharpe_ratio:.2f}")
    c5.metric("卡玛比率", f"{result.calmar_ratio:.2f}")
    c6.metric("胜率", f"{result.win_rate:.1f}%")

    # 自然语言结论
    _show_nlp_summary(result)

    # 第二行指标（Alpha/Beta 需要基准数据才有值）
    if hasattr(result, 'alpha') and result.alpha != 0:
        c7, c8, c9, c10 = st.columns(4)
        c7.metric("Alpha", f"{result.alpha:.2f}%")
        c8.metric("Beta", f"{result.beta:.2f}")
        c9.metric("交易次数", f"{result.trade_count}")
        c10.metric("初始资金", f"¥{result.initial_capital:,.0f}")
    else:
        c7, c8 = st.columns(2)
        c7.metric("交易次数", f"{result.trade_count}")
        c8.metric("初始资金", f"¥{result.initial_capital:,.0f}")

    # 权益曲线（含基准）
    if result.equity_curve:
        eq_df = pd.DataFrame(result.equity_curve)
        initial = eq_df["equity"].iloc[0]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=eq_df["date"], y=eq_df["equity"],
            mode="lines", name="策略权益",
            line=dict(color="#FF6B6B", width=2),
            fill="tozeroy", fillcolor="rgba(255,107,107,0.1)",
        ))

        # 基准曲线
        if benchmark_curve:
            bm_df = pd.DataFrame(benchmark_curve)
            # 归一化到同一起点
            bm_normalized = bm_df["equity"] / bm_df["equity"].iloc[0] * initial
            fig.add_trace(go.Scatter(
                x=bm_df["date"], y=bm_normalized,
                mode="lines", name="沪深300 (归一化)",
                line=dict(width=2, dash="dash", color="gray"),
            ))

        fig.update_layout(
            title="权益曲线",
            template="plotly_dark",
            height=400,
            xaxis_title="日期", yaxis_title="权益(元)",
            xaxis=dict(type="category", nticks=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, width='stretch')

    # 绩效归因
    if result.equity_curve and len(result.equity_curve) > 20:
        _show_performance_attribution(result)

    # 交易记录
    if result.trades:
        st.subheader("📋 交易记录")
        trade_data = []
        for t in result.trades:
            trade_data.append({
                "日期": t.trade_date,
                "股票": t.ts_code,
                "方向": t.direction,
                "价格": f"{t.price:.2f}",
                "数量": t.volume,
                "佣金": f"{t.commission:.2f}",
                "印花税": f"{t.tax:.2f}",
                "盈亏": f"{t.pnl:.2f}" if t.direction == "SELL" else "",
                "持仓天数": t.holding_days if t.direction == "SELL" else "",
            })
            chinese_dataframe(pd.DataFrame(trade_data))

    # 导出报告
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    with col1:
        # 导出完整结果的CSV
        if result.equity_curve:
            eq_df = pd.DataFrame(result.equity_curve)
            csv_data = eq_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 导出权益曲线 CSV",
                data=csv_data,
                file_name=f"回测_{result.strategy}_{result.start_date}_{result.end_date}_权益曲线.csv",
                mime="text/csv",
                width='stretch',
            )
    with col2:
        if result.trades:
            trade_export = []
            for t in result.trades:
                trade_export.append({
                    "trade_date": t.trade_date,
                    "ts_code": t.ts_code,
                    "direction": t.direction,
                    "price": t.price,
                    "volume": t.volume,
                    "commission": t.commission,
                    "tax": t.tax,
                    "pnl": t.pnl,
                    "holding_days": t.holding_days,
                })
            csv_trades = pd.DataFrame(trade_export).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 导出交易明细 CSV",
                data=csv_trades,
                file_name=f"回测_{result.strategy}_交易明细.csv",
                mime="text/csv",
                width='stretch',
            )
    with col3:
        # 汇总报告
        report = f"""回测报告
========
策略: {result.strategy}
参数: {result.params}
日期范围: {result.start_date} ~ {result.end_date}

核心指标
--------
总收益: {result.total_return:.2f}%
年化收益: {result.annual_return:.2f}%
最大回撤: {result.max_drawdown:.2f}%
夏普比率: {result.sharpe_ratio:.2f}
胜率: {result.win_rate:.1f}%
交易次数: {result.trade_count}
初始资金: ¥{result.initial_capital:,.0f}
最终资金: ¥{result.final_capital:,.0f}

生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        st.download_button(
            "📝 导出文本报告 TXT",
            data=report.encode("utf-8-sig"),
            file_name=f"回测报告_{result.strategy}_{result.start_date}.txt",
            mime="text/plain",
            width='stretch',
        )


def _show_performance_attribution(result):
    """绩效归因：月度热力图 + 逐年收益 + 滚动指标"""
    eq_df = pd.DataFrame(result.equity_curve)
    eq_df["trade_date"] = eq_df["date"]
    eq_df["date_parsed"] = pd.to_datetime(eq_df["date"], format="%Y%m%d", errors="coerce")
    eq_df = eq_df.dropna(subset=["date_parsed"])

    if eq_df.empty:
        return

    # 计算每日收益率
    eq_df["daily_return"] = eq_df["equity"].pct_change()

    st.subheader("📈 绩效归因")

    tab_a, tab_b, tab_c = st.tabs(["月度收益热力图", "逐年收益", "滚动指标"])

    with tab_a:
        _draw_monthly_heatmap(eq_df)

    with tab_b:
        _draw_yearly_returns(eq_df)

    with tab_c:
        _draw_rolling_metrics(eq_df)


def _draw_monthly_heatmap(eq_df):
    """月度收益热力图"""
    eq_df["year"] = eq_df["date_parsed"].dt.year
    eq_df["month"] = eq_df["date_parsed"].dt.month

    monthly_ret = eq_df.groupby(["year", "month"])["daily_return"].apply(
        lambda x: (1 + x).prod() - 1
    ).reset_index()
    monthly_ret.columns = ["year", "month", "return"]

    if monthly_ret.empty:
        st.info("数据不足")
        return

    years = sorted(monthly_ret["year"].unique())
    months = list(range(1, 13))

    heat_data = np.full((len(years), 12), np.nan)
    for _, row in monthly_ret.iterrows():
        yi = years.index(row["year"])
        mi = int(row["month"]) - 1
        heat_data[yi][mi] = row["return"] * 100

    month_labels = ["1月", "2月", "3月", "4月", "5月", "6月",
                    "7月", "8月", "9月", "10月", "11月", "12月"]

    fig = go.Figure(data=go.Heatmap(
        z=heat_data,
        x=month_labels,
        y=[str(y) for y in years],
        colorscale="RdYlGn",
        zmid=0,
        text=np.where(np.isnan(heat_data), "", np.round(heat_data, 1)),
        texttemplate="%{text}%",
        hovertemplate="%{y} %{x}<br>收益: %{z:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        title="月度收益率 (%)",
        template="plotly_dark",
        height=200 + len(years) * 40,
        xaxis_title="月份", yaxis_title="年份",
    )
    st.plotly_chart(fig, width='stretch')

    # 汇总统计
    col1, col2, col3 = st.columns(3)
    monthly_stats = monthly_ret["return"] * 100
    col1.metric("月均收益", f"{monthly_stats.mean():.2f}%")
    col2.metric("盈利月数", f"{(monthly_stats > 0).sum()}/{len(monthly_stats)}")
    col3.metric("最佳月份", f"{monthly_stats.max():.2f}%")


def _draw_yearly_returns(eq_df):
    """逐年收益"""
    eq_df["year"] = eq_df["date_parsed"].dt.year
    yearly_ret = eq_df.groupby("year")["daily_return"].apply(
        lambda x: (1 + x).prod() - 1
    ) * 100

    if yearly_ret.empty:
        st.info("数据不足")
        return

    years = yearly_ret.index.tolist()
    returns = yearly_ret.values

    colors = ["red" if r >= 0 else "green" for r in returns]

    fig = go.Figure(data=go.Bar(
        x=[str(y) for y in years],
        y=returns,
        marker_color=colors,
        text=[f"{r:+.1f}%" for r in returns],
        textposition="outside",
    ))
    fig.update_layout(
        title="逐年收益率",
        template="plotly_dark",
        height=350,
        xaxis_title="年份", yaxis_title="收益率 (%)",
        yaxis=dict(zeroline=True, zerolinecolor="gray"),
    )
    st.plotly_chart(fig, width='stretch')


def _draw_rolling_metrics(eq_df):
    """滚动指标（滚动夏普、滚动回撤）"""
    if len(eq_df) < 60:
        st.info("数据不足60个交易日，无法计算滚动指标")
        return

    returns = eq_df["daily_return"].dropna().values

    # 滚动夏普（60日窗口）
    window = min(60, len(returns) // 2)
    rolling_sharpe = []
    rolling_dates = []
    for i in range(window, len(returns)):
        r = returns[i - window:i]
        if r.std() > 0:
            sharpe = (r.mean() * 252) / (r.std() * np.sqrt(252))
        else:
            sharpe = 0
        rolling_sharpe.append(sharpe)
        rolling_dates.append(eq_df["date_parsed"].iloc[i + 1] if i + 1 < len(eq_df) else eq_df["date_parsed"].iloc[-1])

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.05,
                        subplot_titles=["滚动夏普比率 (60日)", "滚动最大回撤 (60日)"])

    fig.add_trace(go.Scatter(
        x=rolling_dates, y=rolling_sharpe,
        mode="lines", name="夏普比率",
        line=dict(color="#4ECDC4", width=2),
        fill="tozeroy", fillcolor="rgba(78,205,196,0.2)",
    ), row=1, col=1)

    # 滚动最大回撤
    rolling_dd = []
    for i in range(window, len(eq_df)):
        segment = eq_df["equity"].iloc[i - window:i]
        peak = segment.cummax()
        dd = ((peak - segment) / peak * 100).max()
        rolling_dd.append(dd)

    fig.add_trace(go.Scatter(
        x=rolling_dates, y=rolling_dd,
        mode="lines", name="最大回撤 (%)",
        line=dict(color="#FF6B6B", width=2),
        fill="tozeroy", fillcolor="rgba(255,107,107,0.2)",
    ), row=2, col=1)

    fig.update_layout(
        template="plotly_dark",
        height=400,
        showlegend=False,
    )
    fig.update_yaxes(title_text="夏普", row=1, col=1)
    fig.update_yaxes(title_text="回撤%", row=2, col=1)
    st.plotly_chart(fig, width='stretch')


def _get_benchmark_curve(start_date: str, end_date: str) -> list:
    """获取沪深300基准收益曲线（从AKShare实时拉取）"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000300")
        if df is None or df.empty:
            # 尝试深交所代码
            df = ak.stock_zh_index_daily(symbol="sz399300")
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "trade_date"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        # 过滤日期范围
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        if df.empty:
            return None
        first_close = df["close"].iloc[0]
        curve = []
        for _, row in df.iterrows():
            curve.append({
                "date": row["trade_date"],
                "equity": row["close"] / first_close * 1000,
            })
        return curve
    except Exception:
        return None


def _show_history():
    """历史回测记录"""
    results_df = get_backtest_results(limit=20)
    if results_df.empty:
        st.info("暂无回测记录")
        return

    for _, row in results_df.iterrows():
        with st.expander(
            f"{row['strategy']} | {row['start_date']}~{row['end_date']} | "
            f"收益 {row['total_return']:.2f}% | 回撤 {row['max_drawdown']:.2f}%"
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("总收益", f"{row['total_return']:.2f}%")
            c2.metric("年化收益", f"{row['annual_return']:.2f}%")
            c3.metric("夏普比率", f"{row['sharpe_ratio']:.2f}")
            c4.metric("胜率", f"{row['win_rate']:.1f}%")

            # 交易明细
            trades_df = get_backtest_trades(row["id"])
            if not trades_df.empty:
                chinese_dataframe(trades_df[["trade_date", "ts_code", "direction",
                                        "price", "volume", "pnl", "holding_days"]])
