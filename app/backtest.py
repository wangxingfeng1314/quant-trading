"""回测中心页面 - 运行回测 + 参数优化 + 多策略对比 + 历史记录"""
import app  # noqa: F401

import streamlit as st
import pandas as pd
import numpy as np
import json
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date

from app.st_utils import (
    chinese_dataframe, chinese_date_picker, strategy_label,
    cached_get_stocks_with_data, cached_instrument_list,
    format_strategy_cn, format_direction_cn, format_stock_cn,
)
from strategies import STRATEGY_REGISTRY, list_strategies
from engine.backtester import (
    Backtester, grid_search, preload_backtest_data, build_backtest_options,
)
from data.storage import (
    get_instrument_list, get_daily, get_backtest_results, get_backtest_trades,
    get_stocks_with_data, get_watchlist, get_backtest_result_by_id,
)
from data.indicators import apply_indicators
from core.config import DEFAULT_CAPITAL


def _render_backtest_options(prefix: str) -> dict:
    """选参 / 样本外验证阶段的撮合规则配置。

    网格搜索和 walk-forward 过去把仓位方式、持仓上限、大盘择时留在默认值上，
    用户在单次回测里配的规则在选参阶段全部失效 —— 挑出来的「最优参数」
    与实盘配置根本不是一回事。这里把同一套规则显式暴露出来并透传下去。
    """
    with st.expander("⚖️ 撮合规则（请与单次回测保持一致）", expanded=False):
        st.caption(
            "选参阶段与样本外验证必须使用与单次回测相同的仓位方式、持仓上限和"
            "大盘择时，否则结论不可比。"
        )
        a, b = st.columns(2)
        with a:
            max_pos = st.number_input(
                "最大持仓只数（0 = 不限）", min_value=0, max_value=30, value=0,
                key=f"{prefix}_max_pos",
            )
            pos_sizing = st.selectbox(
                "仓位方式", ["score", "atr_risk"],
                format_func=lambda x: {"score": "评分阶梯定仓", "atr_risk": "ATR 风险平价"}.get(x, x),
                key=f"{prefix}_pos_sizing",
            )
        with b:
            risk_pct = st.slider(
                "单笔风险占净值（%，仅 ATR 模式）", 0.5, 5.0, 1.5, 0.1,
                key=f"{prefix}_risk_pct",
            )
            regime = st.checkbox("大盘牛熊择时", value=False, key=f"{prefix}_regime")
        index_code = st.text_input(
            "择时基准指数", value="000300.SH", key=f"{prefix}_index",
        )
    return build_backtest_options(
        max_active_positions=int(max_pos),
        position_sizing=pos_sizing,
        risk_per_trade=float(risk_pct) / 100.0,
        market_regime_filter=bool(regime),
        market_index=(index_code or "").strip() or "000300.SH",
    )


def show():
    st.title("🔬 回测中心")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["运行回测", "参数优化", "样本外验证", "多策略对比", "历史记录"]
    )

    with tab1:
        _show_run_backtest()
    with tab2:
        _show_grid_search()
    with tab3:
        _show_validation()
    with tab4:
        _show_multi_strategy()
    with tab5:
        _show_history()


def _show_run_backtest():
    """运行回测（单次）"""
    stock_df = cached_instrument_list()
    stocks_with_data = cached_get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无标的数据，请先运行初始化脚本")
        return

    # 过滤：只保留有数据的标的（避免用户选到无数据的标的）
    stock_df_data = stock_df[stock_df["ts_code"].isin(stocks_with_data)].copy()
    if stock_df_data.empty:
        st.warning("暂无足够行情数据的标的（需至少60条日线），请先下载数据")
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
            format_func=lambda x: strategy_label(
                x, strategy_descs[x],
                getattr(STRATEGY_REGISTRY[x], "style", "综合"),
            ),
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
                    min_val = pinfo.get("min_value", min(0, default) if default <= 0 else 1)
                    max_val = pinfo.get("max_value", None)
                    params[pname] = st.number_input(
                        desc, value=default, min_value=min_val, max_value=max_val, step=1, key=f"bt_param_{pname}"
                    )
                elif isinstance(default, float):
                    min_val = pinfo.get("min_value", None)
                    max_val = pinfo.get("max_value", None)
                    params[pname] = st.number_input(
                        desc, value=default, min_value=min_val, max_value=max_val, step=0.1, key=f"bt_param_{pname}"
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

        mode = st.radio("股票范围", ["单只股票", "多选股票", "全部自选股"], horizontal=True, key="bt_mode")

        if mode == "单只股票":
            search = st.text_input("搜索标的", placeholder="代码或名称", key="bt_search")
            if search:
                mask = (stock_df_data["ts_code"].str.contains(search, case=False, regex=False) |
                        stock_df_data["name"].str.contains(search, case=False, regex=False))
                filtered = stock_df_data[mask]
            else:
                filtered = stock_df_data.head(50)

            options = filtered.apply(
                lambda r: f"[{r['type']}] {r['ts_code']} {r['name']}", axis=1
            ).tolist()

            if options:
                selected = st.selectbox("选择标的", options, key="bt_stock")
                parts = selected.split(" ")
                ts_code = parts[1] if len(parts) >= 2 else parts[0]
                universe = [ts_code]
            else:
                st.warning(f"未找到匹配标的（当前仅有 {len(stock_df_data)} 个标的有数据）")
                universe = []
        elif mode == "全部自选股":
            watchlist = get_watchlist()
            universe = watchlist["ts_code"].tolist() if not watchlist.empty else []
            if not universe:
                st.warning("自选股列表为空，请先添加自选股")
            else:
                wl_eval_mode = st.radio(
                    "自选股回测策略分配",
                    ["⭐ 智能自适应匹配 (推荐·每只标的采用AI专属最高胜率策略)", "统一策略回测 (所有自选股均使用左侧选定策略)"],
                    index=0,
                    key="bt_wl_eval_mode",
                    help="智能自适应模式下，系统会为每只自选股自动匹配其专属胜率最高的量化策略（如白马匹配布林反转、成长股匹配双均线趋势），并叠加牛熊择时与保本止损，实现最高胜率与最低回撤。"
                )
                st.caption(f"📊 将对 {len(universe)} 只自选股逐只回测并呈现全维度胜率与收益分析")
        else:
            all_codes = stock_df_data["ts_code"].tolist()
            name_map = dict(zip(stock_df_data["ts_code"], stock_df_data["name"]))
            type_map = dict(zip(stock_df_data["ts_code"], stock_df_data["type"]))

            watchlist = get_watchlist()
            wl_codes = [c for c in watchlist["ts_code"].tolist() if c in all_codes] if not watchlist.empty else []
            extra_codes = [c for c in all_codes if c not in wl_codes]
            core_10 = [c for c in ['600900.SH', '600036.SH', '601318.SH', '600660.SH', '600690.SH', '600887.SH', '600089.SH', '000725.SZ', '002241.SZ', '600941.SH'] if c in all_codes]

            # 候选池过滤：默认严格仅展示当前自选股（若自选股有数据）
            show_extra = False
            if extra_codes:
                show_extra = st.checkbox(
                    f"🌐 包含历史已下载的其他标的 (+{len(extra_codes)}只)",
                    value=False,
                    key="bt_show_extra",
                    help="默认仅展示当前自选股列表。勾选后可搜索并选入之前下载过历史K线的其他标的。"
                )

            candidate_codes = all_codes if show_extra or not wl_codes else wl_codes

            def format_bt_label(c):
                tag = type_map.get(c, '标的')
                if show_extra and extra_codes:
                    src_tag = "自选" if c in wl_codes else "历史"
                    return f"[{src_tag}|{tag}] {c} {name_map.get(c, '')}"
                return f"[{tag}] {c} {name_map.get(c, '')}"

            # 快捷操作栏
            btn_col1, btn_col2, btn_col3 = st.columns([1.1, 1.2, 0.9])
            with btn_col1:
                if st.button("➕ 选入自选股", key="bt_quick_wl", help=f"一键将 {len(wl_codes)} 只自选股加入多选框"):
                    st.session_state["bt_multiselect"] = sorted(list(set(st.session_state.get("bt_multiselect", []) + wl_codes)))
                    st.rerun()
            with btn_col2:
                if st.button("⚡ 选入核心白马", key="bt_quick_core", help="一键选入核心白马股池"):
                    available_core = [c for c in core_10 if c in candidate_codes]
                    st.session_state["bt_multiselect"] = sorted(list(set(st.session_state.get("bt_multiselect", []) + available_core)))
                    st.rerun()
            with btn_col3:
                if st.button("🗑️ 清空已选", key="bt_quick_clear"):
                    st.session_state["bt_multiselect"] = []
                    st.rerun()

            # 多选默认值：确保仅包含在 candidate_codes 范围内的合法代码
            current_selected = [c for c in st.session_state.get("bt_multiselect", []) if c in candidate_codes]
            if not current_selected and "bt_multiselect" not in st.session_state:
                current_selected = wl_codes[:5] if wl_codes else candidate_codes[:5]

            # 多选框主体（支持输入代码或名称模糊搜索、多项勾选）
            selected_codes = st.multiselect(
                "选择多只股票（多选框·支持输入代码/中文名称快速搜索）",
                options=candidate_codes,
                default=current_selected,
                format_func=format_bt_label,
                key="bt_multiselect",
                placeholder="🔍 点击此处或直接输入股票代码/名称搜索多选..."
            )
            universe = selected_codes

            with st.expander("📝 文本批量粘贴导入 (可选)", expanded=False):
                manual_text = st.text_area("粘贴股票代码（支持换行/逗号/空格分隔）", height=70, key="bt_manual_paste",
                                           placeholder="600519.SH\n000001.SZ\n601318.SH")
                if st.button("📥 导入到上方多选框", key="bt_do_import"):
                    import re
                    pasted = [re.sub(r'[^0-9a-zA-Z\.]', '', c).strip() for c in re.split(r'[\r\n,\s]+', manual_text) if c.strip()]
                    valid_pasted = [c for c in pasted if c in all_codes]
                    if valid_pasted:
                        st.session_state["bt_multiselect"] = sorted(list(set(selected_codes + valid_pasted)))
                        st.success(f"成功导入 {len(valid_pasted)} 只标的")
                        st.rerun()
                    else:
                        st.warning("未解析到包含有效数据的股票代码")

            if universe:
                names_summary = "、".join([name_map.get(c, c) for c in universe[:4]])
                if len(universe) > 4:
                    names_summary += f" 等 {len(universe)} 只标的"
                st.caption(f"📊 已选标的组合 ({len(universe)} 只): **{names_summary}**")
            else:
                st.warning("⚠️ 请在多选框中至少选择一只股票/ETF")

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

        # 高阶实盘与风控设置
        with st.expander("⚙️ 高阶撮合与独立风控设置", expanded=False):
            exec_mode_opt = st.radio(
                "撮合时序模式",
                ["次日开盘价撮合 (next_open，推荐防假定成交)", "当日收盘价撮合 (current_close)"],
                index=0,
                key="bt_exec_mode",
            )
            exec_mode = "next_open" if "next_open" in exec_mode_opt else "current_close"

            enable_regime = st.checkbox(
                "🛡️ 启用大盘牛熊择时网关 (熊市防守期自动拦截逆势买入，防假突破，大幅压降回撤)",
                value=True,
                key="bt_enable_regime",
                help="当沪深300指数处于60日均线下方且处于空头下行时，自动禁止开多仓，保护本金并降低最大回撤。"
            )

            enable_rm = st.checkbox("启用独立出场风控管理器 (RiskManager)", value=True, key="bt_enable_rm")
            rm_stop_loss = -5.0
            rm_trail_start = 8.0
            rm_trail_callback = 3.0
            rm_max_days = 0
            enable_be = True
            be_trigger = 5.0
            be_buffer = 0.2
            if enable_rm:
                rm_c1, rm_c2 = st.columns(2)
                with rm_c1:
                    stop_loss_pct = st.number_input("硬止损阈值 (%)", value=6.0, min_value=1.0, max_value=20.0, step=0.5, key="bt_sl_pct")
                    rm_stop_loss = -abs(float(stop_loss_pct))
                    trail_start_pct = st.number_input("移动止盈启动浮盈 (%)", value=12.0, min_value=2.0, max_value=50.0, step=1.0, key="bt_ts_pct")
                    rm_trail_start = float(trail_start_pct)
                with rm_c2:
                    trail_pullback_pct = st.number_input("移动止盈高点回撤 (%)", value=4.0, min_value=1.0, max_value=20.0, step=0.5, key="bt_tp_pct")
                    rm_trail_callback = float(trail_pullback_pct)
                    max_days_val = st.number_input("最长持仓天数 (0=不限)", value=0, min_value=0, max_value=250, step=5, key="bt_max_days", help="推荐设为0(不强制平仓)，避免波段策略被30天硬中断")
                    rm_max_days = int(max_days_val)

                # 保本止损机制
                enable_be = st.checkbox("🛡️ 启用保本止损保护 (浮盈达标后回撤保本，锁定本金不转亏)", value=True, key="bt_enable_be")
                if enable_be:
                    bec1, bec2 = st.columns(2)
                    with bec1:
                        be_trigger = st.number_input("保本激活浮盈 (%)", value=5.0, min_value=1.0, max_value=30.0, step=0.5, key="bt_be_trigger", help="浮盈达到该百分比后，开启成本保本守护")
                    with bec2:
                        be_buffer = st.number_input("保本平仓缓冲 (%)", value=0.2, min_value=0.0, max_value=2.0, step=0.1, key="bt_be_buffer", help="回撤至成本+缓冲线时平仓锁定本金（覆盖手续费）")

            # 单只股票回测默认 1 (全仓)，多只股票组合默认 4
            default_max_pos = 1 if (mode == "单只标的" or len(universe) <= 1) else 4
            max_pos_val = st.number_input("最大并发持仓股票数 (0=不限)", value=default_max_pos, min_value=0, max_value=50, step=1, key="bt_max_pos", help="单只标的回测请保持1以全额使用资金，多只股票组合建议设为3~5只")

            pos_sizing_opt = st.radio(
                "仓位分配模型",
                ["按信号评分阶梯定仓 (score)", "ATR波动率风险平价定仓 (atr_risk，高波动少买/低波动多买)"],
                index=0,
                horizontal=True,
                key="bt_pos_sizing",
            )
            pos_sizing = "atr_risk" if "atr_risk" in pos_sizing_opt else "score"

    if st.button("🚀 运行回测", type="primary", width='stretch'):
        if not universe:
            st.error("请选择至少一只股票")
            return

        from engine.risk_manager import RiskManager
        rm_instance = None
        if enable_rm:
            rm_instance = RiskManager(
                stop_loss_pct=rm_stop_loss,
                trailing_stop_activation=rm_trail_start,
                trailing_stop_callback=rm_trail_callback,
                max_holding_days=rm_max_days,
                enable_break_even=enable_be,
                break_even_trigger_pct=be_trigger,
                break_even_buffer_pct=be_buffer,
            )

        # 批量回测自选股模式
        if mode == "全部自选股":
            results = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            watchlist = get_watchlist()
            wl_dict = dict(zip(watchlist["ts_code"], watchlist["recommended_strategy"])) if not watchlist.empty and "recommended_strategy" in watchlist.columns else {}

            for i, ts_code in enumerate(universe):
                status_text.text(f"回测中 {i+1}/{len(universe)}: {ts_code}")
                # 判定采用的策略类与参数
                cur_strategy_cls = strategy_cls
                cur_params = params
                cur_strat_label = format_strategy_cn(strategy_cls.name)

                if 'wl_eval_mode' in locals() and wl_eval_mode.startswith("⭐"):
                    # 智能自适应匹配模式
                    rec_str = wl_dict.get(ts_code, "")
                    matched_key = None
                    if "布林" in rec_str or "bollinger" in rec_str:
                        matched_key = "bollinger_reversal"
                    elif "双均线" in rec_str or "ma_cross" in rec_str:
                        matched_key = "ma_cross"
                    elif "KDJ" in rec_str or "kdj" in rec_str:
                        matched_key = "kdj_cross"
                    elif "多因子" in rec_str or "combo" in rec_str:
                        matched_key = "signal_combo"

                    if not matched_key:
                        from services.strategy_recommender import evaluate_stock_strategy
                        inst_df = cached_instrument_list()
                        nm = ""
                        ind = ""
                        if not inst_df.empty and ts_code in inst_df["ts_code"].values:
                            r = inst_df[inst_df["ts_code"] == ts_code].iloc[0]
                            nm = r.get("name", "")
                            ind = r.get("industry", "")
                        ev = evaluate_stock_strategy(ts_code, name=nm, industry=ind, start_date=start_date, end_date=end_date)
                        matched_key = ev.get("strategy", "kdj_cross")

                    if matched_key in STRATEGY_REGISTRY:
                        cur_strategy_cls = STRATEGY_REGISTRY[matched_key]
                        cur_params = {k: v.get("default", 0) for k, v in cur_strategy_cls.param_schema.items()}
                        cur_strat_label = format_strategy_cn(matched_key)

                bt = Backtester(
                    strategy_cls=cur_strategy_cls, params=cur_params,
                    universe=[ts_code],
                    start_date=start_date, end_date=end_date,
                    initial_capital=float(capital),
                    execution_mode=exec_mode,
                    risk_manager=rm_instance,
                    max_active_positions=int(max_pos_val),
                    position_sizing=pos_sizing,
                    market_regime_filter=enable_regime,
                )
                result = bt.run(save=False)
                if result.equity_curve:
                    results.append({
                        "ts_code": ts_code,
                        "strategy_name": cur_strat_label,
                        "result": result,
                    })
                progress_bar.progress((i + 1) / len(universe))

            progress_bar.empty()
            status_text.empty()

            if not results:
                st.warning("所有自选股均无交易产生")
                return

            st.success(f"回测完成！{len(results)}/{len(universe)} 只自选股产生交易")

            # 对比表格
            stock_df = cached_instrument_list()
            name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}
            compare_rows = []
            for r in results:
                res = r["result"]
                compare_rows.append({
                    "股票": f"{r['ts_code']} {name_map.get(r['ts_code'], '')}",
                    "采用策略": r.get("strategy_name", ""),
                    "总收益%": float(res.total_return),
                    "年化%": float(res.annual_return),
                    "夏普": float(res.sharpe_ratio),
                    "最大回撤%": float(res.max_drawdown),
                    "胜率%": float(res.win_rate),
                    "盈亏比": float(getattr(res, "profit_factor", 1.0)),
                    "交易次数": int(res.trade_count),
                })

            df_compare = pd.DataFrame(compare_rows)
            st.subheader("📊 自选股回测对比与胜率大屏")

            # 汇总核心卡片
            avg_win = df_compare["胜率%"].mean()
            avg_ret = df_compare["总收益%"].mean()
            avg_mdd = df_compare["最大回撤%"].mean()
            avg_pf = df_compare["盈亏比"].mean()

            sm1, sm2, sm3, sm4 = st.columns(4)
            sm1.metric("组合平均胜率", f"{avg_win:.1f}%")
            sm2.metric("标的平均收益", f"{avg_ret:+.2f}%")
            sm3.metric("平均最大回撤", f"{avg_mdd:.2f}%")
            sm4.metric("平均盈亏比", f"{avg_pf:.2f}")

            styled_compare = (
                df_compare.style.highlight_max(subset=["总收益%", "夏普", "胜率%", "盈亏比"], color="#90EE90")
                                .highlight_min(subset=["最大回撤%"], color="#90EE90")
                                .format({
                                    "总收益%": "{:+.2f}%",
                                    "年化%": "{:+.2f}%",
                                    "夏普": "{:.2f}",
                                    "最大回撤%": "{:.2f}%",
                                    "胜率%": "{:.1f}%",
                                    "盈亏比": "{:.2f}",
                                })
            )
            st.dataframe(styled_compare, hide_index=True, width="stretch")

            # 最佳结果展示
            best = max(results, key=lambda r: r["result"].total_return)
            st.subheader(f"🏆 最佳表现: {best['ts_code']} {name_map.get(best['ts_code'], '')} ({best.get('strategy_name', '')})")
            st.caption(f"总收益 {best['result'].total_return:+.2f}% | "
                       f"夏普 {best['result'].sharpe_ratio:.2f} | "
                       f"胜率 {best['result'].win_rate:.1f}% | "
                       f"最大回撤 {best['result'].max_drawdown:.2f}%")
            return

        # 单只/多只模式
        with st.spinner("正在回测中..."):
            bt = Backtester(
                strategy_cls=strategy_cls,
                params=params,
                universe=universe,
                start_date=start_date, end_date=end_date,
                initial_capital=float(capital),
                execution_mode=exec_mode,
                risk_manager=rm_instance,
                max_active_positions=int(max_pos_val),
                position_sizing=pos_sizing,
                market_regime_filter=enable_regime,
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
    stock_df = cached_instrument_list()
    stocks_with_data = cached_get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无标的数据")
        return

    # 只保留有数据的标的
    stock_df_data = stock_df[stock_df["ts_code"].isin(stocks_with_data)].copy()
    if stock_df_data.empty:
        st.warning("暂无足够行情数据的标的（需至少60条日线），请先下载数据")
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
            format_func=lambda x: strategy_label(
                x, strategy_descs[x],
                getattr(STRATEGY_REGISTRY[x], "style", "综合"),
            ),
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
            if default >= 0:
                calc_min = int(default * 0.5) if val_type == "int" else round(default * 0.5, 2)
                calc_max = int(default * 2) if val_type == "int" else round(default * 2, 2)
            else:
                calc_min = int(default * 2) if val_type == "int" else round(default * 2, 2)
                calc_max = int(default * 0.5) if val_type == "int" else round(default * 0.5, 2)
            if calc_min == calc_max:
                calc_min -= 1
                calc_max += 1

            min_v = st.number_input("最小值", value=calc_min, key=f"gs_min_{pname}")
            max_v = st.number_input("最大值", value=calc_max, key=f"gs_max_{pname}")
            steps = st.number_input("步数", value=5, min_value=2, max_value=20, key=f"gs_steps_{pname}")

            low_v, high_v = min(min_v, max_v), max(min_v, max_v)
            if val_type == "int":
                step_size = max(1, int(round((high_v - low_v) / max(1, steps - 1))))
                param_grid[pname] = sorted(list(set(range(int(low_v), int(high_v) + 1, step_size))))
            else:
                step_size = (high_v - low_v) / max(1, steps - 1)
                param_grid[pname] = sorted(list(set([round(low_v + i * step_size, 2) for i in range(int(steps))])))

    # 股票选择
    mode = st.radio("标的", ["单只标的", "多选标的"], horizontal=True, key="gs_mode")
    if mode == "单只标的":
        gs_search = st.text_input("搜索标的", placeholder="输入代码或中文名称过滤", key="gs_search")
        if gs_search:
            mask = (stock_df_data["ts_code"].str.contains(gs_search, case=False, regex=False) |
                    stock_df_data["name"].str.contains(gs_search, case=False, regex=False))
            filtered = stock_df_data[mask]
        else:
            filtered = stock_df_data.head(100)

        options = filtered.apply(
            lambda r: f"[{r['type']}] {r['ts_code']} {r['name']}", axis=1
        ).tolist()
        if options:
            selected = st.selectbox("选择标的", options, key="gs_stock")
            parts = selected.split(" ")
            ts_code = parts[1] if len(parts) >= 2 else parts[0]
            universe = [ts_code]
        else:
            st.warning("未找到匹配标的，请调整搜索词")
            return
    else:
        all_codes = stock_df_data["ts_code"].tolist()
        name_map = dict(zip(stock_df_data["ts_code"], stock_df_data["name"]))
        type_map = dict(zip(stock_df_data["ts_code"], stock_df_data["type"]))

        watchlist = get_watchlist()
        wl_codes = [c for c in watchlist["ts_code"].tolist() if c in all_codes] if not watchlist.empty else []
        extra_codes = [c for c in all_codes if c not in wl_codes]
        core_10 = [c for c in ['600900.SH', '600036.SH', '601318.SH', '600660.SH', '600690.SH', '600887.SH', '600089.SH', '000725.SZ', '002241.SZ', '600941.SH'] if c in all_codes]

        show_gs_extra = False
        if extra_codes:
            show_gs_extra = st.checkbox(
                f"🌐 包含历史已下载的其他标的 (+{len(extra_codes)}只)",
                value=False,
                key="gs_show_extra",
                help="默认仅展示当前自选股列表。勾选后可搜索并选入之前下载过历史K线的其他标的。"
            )

        candidate_codes = all_codes if show_gs_extra or not wl_codes else wl_codes

        def format_gs_label(c):
            tag = type_map.get(c, '标的')
            if show_gs_extra and extra_codes:
                src_tag = "自选" if c in wl_codes else "历史"
                return f"[{src_tag}|{tag}] {c} {name_map.get(c, '')}"
            return f"[{tag}] {c} {name_map.get(c, '')}"

        gcol1, gcol2, gcol3 = st.columns([1.1, 1.2, 0.9])
        with gcol1:
            if st.button("➕ 选入自选股", key="gs_quick_wl"):
                st.session_state["gs_multiselect"] = sorted(list(set(st.session_state.get("gs_multiselect", []) + wl_codes)))
                st.rerun()
        with gcol2:
            if st.button("⚡ 选入核心白马", key="gs_quick_core"):
                available_core = [c for c in core_10 if c in candidate_codes]
                st.session_state["gs_multiselect"] = sorted(list(set(st.session_state.get("gs_multiselect", []) + available_core)))
                st.rerun()
        with gcol3:
            if st.button("🗑️ 清空标的", key="gs_quick_clear"):
                st.session_state["gs_multiselect"] = []
                st.rerun()

        current_gs_selected = [c for c in st.session_state.get("gs_multiselect", []) if c in candidate_codes]
        if not current_gs_selected and "gs_multiselect" not in st.session_state:
            current_gs_selected = wl_codes[:3] if wl_codes else candidate_codes[:2]

        selected_codes = st.multiselect(
            "选择多只标的（多选框·支持输入代码/中文名称搜索）",
            options=candidate_codes,
            default=current_gs_selected,
            format_func=format_gs_label,
            key="gs_multiselect",
            placeholder="🔍 点击此处或直接输入代码/名称搜索多只标的..."
        )
        universe = selected_codes

        with st.expander("📝 文本批量粘贴导入 (可选)", expanded=False):
            gs_manual_text = st.text_area("粘贴标的代码（支持换行/逗号/空格分隔）", height=70, key="gs_manual_paste",
                                          placeholder="600519.SH\n000001.SZ\n601318.SH")
            if st.button("📥 导入到上方多选框", key="gs_do_import"):
                import re
                pasted = [re.sub(r'[^0-9a-zA-Z\.]', '', c).strip() for c in re.split(r'[\r\n,\s]+', gs_manual_text) if c.strip()]
                valid_pasted = [c for c in pasted if c in all_codes]
                if valid_pasted:
                    st.session_state["gs_multiselect"] = sorted(list(set(selected_codes + valid_pasted)))
                    st.success(f"成功导入 {len(valid_pasted)} 只标的")
                    st.rerun()
                else:
                    st.warning("未解析到包含有效数据的股票代码")

        if universe:
            st.caption(f"📊 已选调优标的 ({len(universe)} 只): **{', '.join([name_map.get(c, c) for c in universe[:4]])}**")
        else:
            st.warning("⚠️ 请在多选框中至少选择一只股票/ETF标的")

    date_col1, date_col2 = st.columns(2)
    with date_col1:
        gs_start = chinese_date_picker("开始日期", default_val=date(2023,1,1), key="gs_start")
    with date_col2:
        gs_end = chinese_date_picker("结束日期", default_val=date.today(), key="gs_end")

    # 总组合数
    total_combos = 1
    for v in param_grid.values():
        total_combos *= len(v)
    gs_opt_c1, gs_opt_c2 = st.columns(2)
    with gs_opt_c1:
        gs_exec_mode_label = st.radio(
            "优化撮合口径",
            ["次日开盘价 (next_open，与实盘一致防过拟合)", "当日收盘价 (current_close)"],
            index=0,
            horizontal=True,
            key="gs_exec_mode",
        )
        gs_exec_mode = "next_open" if "next_open" in gs_exec_mode_label else "current_close"
    with gs_opt_c2:
        gs_enable_rm = st.checkbox("同步开启出场风控 (RiskManager: -6%止损 / +12%回撤4%止盈)", value=True, key="gs_enable_rm")
    gs_options = _render_backtest_options("gs")
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

        gs_rm = None
        if gs_enable_rm:
            from engine.risk_manager import RiskManager
            gs_rm = RiskManager(stop_loss_pct=-6.0, trailing_stop_activation=12.0, trailing_stop_callback=4.0)

        results = grid_search(
            strategy_cls=strategy_cls,
            universe=universe,
            start_date=gs_start, end_date=gs_end,
            initial_capital=DEFAULT_CAPITAL,
            param_grid=param_grid,
            metric=metric.replace("-", ""),
            progress_callback=on_progress,
            execution_mode=gs_exec_mode,
            risk_manager=gs_rm,
            backtest_options=gs_options,
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


# ============================================================
# 样本外验证（walk-forward / 参数稳定性 / 蒙特卡洛）
# ============================================================

def _show_validation():
    """样本外验证：把历史切成多折，前段挑参数、后段验证，回答「这份收益能不能延续」"""
    from engine.validation import walk_forward

    stock_df = cached_instrument_list()
    stocks_with_data = cached_get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无标的数据")
        return
    stock_df_data = stock_df[stock_df["ts_code"].isin(stocks_with_data)].copy()
    if stock_df_data.empty:
        st.warning("暂无足够行情数据的标的（需至少60条日线），请先下载数据")
        return

    st.subheader("🧪 样本外验证")
    st.caption(
        "参数优化得到的「最优参数」是在同一段历史上挑出来的，天然偏乐观。"
        "本页把历史切成 n 折：每折只用前段数据挑参数，再在紧随其后的、"
        "从未参与选参的样本外段上运行，最后把各折样本外曲线拼接，看真实表现。"
    )

    # ---------- 配置区 ----------
    c1, c2 = st.columns(2)
    with c1:
        strategies = list_strategies()
        strategy_names = [s["name"] for s in strategies]
        strategy_descs = {s["name"]: s["desc"] for s in strategies}
        selected_strategy = st.selectbox(
            "选择策略",
            strategy_names,
            format_func=lambda x: strategy_label(
                x, strategy_descs[x], getattr(STRATEGY_REGISTRY[x], "style", "综合"),
            ),
            key="wf_strategy",
        )
        strategy_cls = STRATEGY_REGISTRY[selected_strategy]
    with c2:
        metric = st.selectbox(
            "选参目标",
            ["total_return", "sharpe_ratio", "calmar_ratio", "max_drawdown"],
            format_func=lambda x: {
                "total_return": "总收益率",
                "sharpe_ratio": "夏普比率",
                "calmar_ratio": "卡玛比率",
                "max_drawdown": "最大回撤（越小越好）",
            }.get(x, x),
            key="wf_metric",
        )

    if not strategy_cls.param_schema:
        st.info("该策略无可调参数，无法做样本外参数验证（可直接看单次回测的样本外切分结果）")
        return

    st.markdown("**参数搜索范围**（每折在这组网格内独立挑参）")
    param_grid = {}
    cols = st.columns(len(strategy_cls.param_schema))
    for i, (pname, pinfo) in enumerate(strategy_cls.param_schema.items()):
        with cols[i]:
            default = pinfo.get("default", 10)
            st.markdown(f"**{pinfo.get('desc', pname)}**")
            is_int = isinstance(default, int)
            if default >= 0:
                calc_min = int(default * 0.5) if is_int else round(default * 0.5, 2)
                calc_max = int(default * 2) if is_int else round(default * 2, 2)
            else:
                calc_min = int(default * 2) if is_int else round(default * 2, 2)
                calc_max = int(default * 0.5) if is_int else round(default * 0.5, 2)
            if calc_min == calc_max:
                calc_min -= 1
                calc_max += 1
            min_v = st.number_input("最小值", value=calc_min, key=f"wf_min_{pname}")
            max_v = st.number_input("最大值", value=calc_max, key=f"wf_max_{pname}")
            steps = st.number_input("步数", value=3, min_value=2, max_value=8, key=f"wf_steps_{pname}")
            low_v, high_v = min(min_v, max_v), max(min_v, max_v)
            if is_int:
                step_size = max(1, int(round((high_v - low_v) / max(1, steps - 1))))
                param_grid[pname] = sorted(set(range(int(low_v), int(high_v) + 1, step_size)))
            else:
                step_size = (high_v - low_v) / max(1, steps - 1)
                param_grid[pname] = sorted(set(round(low_v + i * step_size, 2) for i in range(int(steps))))

    total_combos = 1
    for v in param_grid.values():
        total_combos *= len(v)

    # ---------- 标的与窗口 ----------
    all_codes = stock_df_data["ts_code"].tolist()
    name_map = dict(zip(stock_df_data["ts_code"], stock_df_data["name"]))
    watchlist = get_watchlist()
    wl_codes = [c for c in watchlist["ts_code"].tolist() if c in all_codes] if not watchlist.empty else []

    wcol1, wcol2 = st.columns([3, 1])
    with wcol1:
        universe = st.multiselect(
            "标的（建议 3 只以上，单只标的的样本外结论噪声极大）",
            options=all_codes,
            default=(wl_codes[:5] if wl_codes else all_codes[:3]),
            format_func=lambda c: f"{c} {name_map.get(c, '')}",
            key="wf_universe",
        )
    with wcol2:
        st.caption("快速选择")
        if st.button("➕ 自选股", key="wf_quick_wl"):
            st.session_state["wf_universe"] = sorted(set(wl_codes))
            st.rerun()

    d1, d2 = st.columns(2)
    with d1:
        wf_start = chinese_date_picker("开始日期", default_val=date(2020, 1, 1), key="wf_start")
    with d2:
        wf_end = chinese_date_picker("结束日期", default_val=date.today(), key="wf_end")

    o1, o2, o3 = st.columns(3)
    with o1:
        n_splits = st.slider("折数", 2, 6, 4, key="wf_splits",
                             help="折数越多，样本外覆盖越完整，但每折训练数据越少")
    with o2:
        train_ratio = st.slider("首折训练占比", 0.5, 0.9, 0.7, 0.05, key="wf_ratio",
                                help="首折训练段占整段历史的比例，之后每折向前滚动一个测试窗宽度")
    with o3:
        wf_enable_rm = st.checkbox("出场风控", value=True, key="wf_enable_rm",
                                   help="-6% 止损 / +12% 启动 4% 回撤止盈")

    wf_options = _render_backtest_options("wf")
    st.caption(
        f"参数组合 {total_combos} 组 × {n_splits} 折 ≈ {total_combos * n_splits} 次回测，"
        f"另加一次全区间网格用于参数稳定性检验"
    )

    if st.button("🧪 开始样本外验证", type="primary", width='stretch', key="wf_run"):
        if not universe:
            st.error("请至少选择一只标的")
            return
        if len(universe) < 3:
            st.warning("标的少于 3 只时，样本外结论的统计意义有限，建议扩大标的池")

        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(done, total):
            if total:
                progress_bar.progress(min(1.0, done / total))
            status_text.text(f"进度 {done}/{total}")

        risk_manager = None
        if wf_enable_rm:
            from engine.risk_manager import RiskManager
            risk_manager = RiskManager(stop_loss_pct=-6.0,
                                       trailing_stop_activation=12.0,
                                       trailing_stop_callback=4.0)

        try:
            report = walk_forward(
                strategy_cls=strategy_cls,
                param_grid=param_grid,
                universe=universe,
                start_date=wf_start,
                end_date=wf_end,
                initial_capital=DEFAULT_CAPITAL,
                n_splits=n_splits,
                train_ratio=float(train_ratio),
                metric=metric,
                execution_mode="next_open",
                risk_manager=risk_manager,
                progress_callback=on_progress,
                backtest_options=wf_options,
            )
        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            st.error(f"验证失败：{e}")
            return

        progress_bar.empty()
        status_text.empty()
        st.session_state["wf_report"] = report
        st.session_state["wf_meta"] = {
            "strategy": selected_strategy,
            "metric": metric,
            "universe": list(universe),
            "start": wf_start,
            "end": wf_end,
            "n_splits": n_splits,
        }

    report = st.session_state.get("wf_report")
    if report is None:
        st.info("配置完成后点击上方按钮开始验证。首次运行需要拉取行情并逐折回测，标的较多时请耐心等待。")
        return

    _render_validation_report(report, st.session_state.get("wf_meta", {}))


def _render_validation_report(report, meta: dict):
    """渲染样本外验证报告"""
    if not report.windows:
        st.error("所有折均未产出有效结果，无法评估")
        for w in report.warnings:
            st.warning(w)
        return

    # ---------- 结论条 ----------
    verdict = report.verdict or ""
    if "不适合实盘" in verdict or "不通过" in verdict or "脆弱" in verdict:
        st.error(f"**结论：{verdict}**")
    elif "谨慎" in verdict or "有限" in verdict:
        st.warning(f"**结论：{verdict}**")
    else:
        st.success(f"**结论：{verdict}**")

    st.caption(
        f"策略 {meta.get('strategy', '-')} | 标的 {len(meta.get('universe', []))} 只 | "
        f"区间 {meta.get('start', '-')}~{meta.get('end', '-')} | "
        f"{len(report.windows)} 折 | 选参目标 {report.metric}"
    )

    # ---------- 核心指标 ----------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("样本外总收益", f"{report.oos_total_return:.2f}%")
    m2.metric("样本外年化", f"{report.oos_annual_return:.2f}%")
    m3.metric("样本外最大回撤", f"{report.oos_max_drawdown:.2f}%")
    m4.metric("样本外夏普", f"{report.oos_sharpe:.2f}")
    if report.is_mean_metric > 1e-9:
        m5.metric("样本外效率", f"{report.efficiency:.0%}",
                  help="样本外均值 / 样本内均值。低于 50% 说明收益主要来自过拟合")
    else:
        m5.metric("样本外效率", "不适用", help="样本内均值为负，效率比无解释力")

    # ---------- 逐折明细 ----------
    st.markdown("**逐折明细**")
    fold_df = pd.DataFrame([w.as_dict() for w in report.windows])
    chinese_dataframe(fold_df, height=260)

    fig_fold = go.Figure()
    fold_labels = [f"第{w.index + 1}折" for w in report.windows]
    fig_fold.add_trace(go.Bar(
        name="样本内（训练段）", x=fold_labels,
        marker_color="#5B8FF9",
        y=[round(w.train_metric, 2) for w in report.windows],
    ))
    fig_fold.add_trace(go.Bar(
        name="样本外（测试段）", x=fold_labels,
        marker_color="#F6BD16",
        y=[round(w.test_metric, 2) for w in report.windows],
    ))
    fig_fold.update_layout(
        barmode="group", height=280, margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        yaxis_title=report.metric,
    )
    st.plotly_chart(fig_fold, width='stretch')

    # ---------- 样本外权益曲线 ----------
    if report.oos_equity:
        st.markdown("**拼接样本外净值曲线**（各折测试段首尾相接，不含任何训练段数据）")
        eq_df = pd.DataFrame(report.oos_equity)
        eq_df["date"] = eq_df["date"].astype(str)
        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=eq_df["date"], y=eq_df["equity"], mode="lines",
            name="样本外净值", line=dict(color="#5AD8A6", width=2),
            fill="tozeroy", fillcolor="rgba(90,216,166,0.12)",
        ))
        peak = eq_df["equity"].cummax()
        fig_eq.add_trace(go.Scatter(
            x=eq_df["date"], y=peak, mode="lines",
            name="历史峰值", line=dict(color="#B0B0B0", width=1, dash="dot"),
        ))
        fig_eq.update_layout(
            height=320, margin=dict(l=10, r=10, t=20, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
            yaxis_title="组合权益（元）",
        )
        st.plotly_chart(fig_eq, width='stretch')

    # ---------- 参数稳定性 ----------
    st.markdown("**参数稳定性检验**")
    ps = report.param_stability or {}
    if not ps or ps.get("robustness") is None:
        st.info("网格过小，无法构造邻居参数（每个参数至少需要 2 个候选值）")
    else:
        rob = float(ps.get("robustness", 0))
        nb = ps.get("neighbor_mean")
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("最优参数指标", f"{ps.get('best_metric', 0):.2f}")
        p2.metric("邻居参数均值", f"{nb:.2f}" if nb is not None else "—")
        p3.metric("稳定性（邻居/最优）", f"{rob:.2f}",
                  help="≥0.7 视为稳健高原；<0.5 说明最优点是孤立尖峰，大概率拟合噪声")
        p4.metric("全网格正收益占比", f"{ps.get('positive_ratio', 0):.0%}")

        if rob >= 0.7:
            st.success(f"参数稳定：把参数挪动一档后指标仍有最优值的 {rob:.0%}，优势不依赖精确点位")
        elif rob >= 0.5:
            st.warning(f"参数中等稳定（{rob:.0%}）：最优参数附近性能已有明显衰减，实盘建议取参数区间中枢")
        else:
            st.error(f"参数脆弱（{rob:.0%}）：最优点是孤立尖峰，改一档参数就失效，属于典型过拟合特征")

        per_param = ps.get("per_param") or {}
        if per_param:
            rows = []
            for pname, info in per_param.items():
                pm = info.get("mean")
                rows.append({
                    "参数": pname,
                    "最优值": info.get("best"),
                    "邻居值": ", ".join(str(v) for v in info.get("neighbors", [])) or "—",
                    "邻居均值": round(float(pm), 2) if pm is not None else "—",
                })
            chinese_dataframe(pd.DataFrame(rows), height=220)

    # ---------- 蒙特卡洛 ----------
    st.markdown("**交易序列蒙特卡洛**")
    mc = report.monte_carlo or {}
    if not mc.get("runs"):
        st.info(mc.get("note", "交易笔数不足，蒙特卡洛无统计意义"))
    else:
        st.caption(
            "左：路径风险 —— 同样的交易换个发生顺序，回撤可能多深。"
            "右：收益不确定性 —— 如果市场给你的是另一批统计特征相同但结果不同的交易，会赚还是亏。"
        )
        g1, g2 = st.columns(2)
        with g1:
            samples = mc.get("drawdown_samples") or []
            if samples:
                fig_dd = go.Figure()
                fig_dd.add_trace(go.Histogram(
                    x=samples, nbinsx=40, marker_color="rgba(246,189,22,0.75)", name="重排回撤分布",
                ))
                fig_dd.add_vline(
                    x=mc.get("observed_realized_drawdown", 0), line_dash="dash",
                    line_color="#E8684A", annotation_text="实际路径", annotation_position="top",
                )
                fig_dd.add_vline(
                    x=mc.get("drawdown_p95", 0), line_dash="dot",
                    line_color="#6DC8EC", annotation_text="95%分位", annotation_position="top",
                )
                fig_dd.update_layout(
                    height=300, margin=dict(l=10, r=10, t=30, b=10),
                    xaxis_title="最大回撤（%）", yaxis_title="频次", showlegend=False,
                )
                st.plotly_chart(fig_dd, width='stretch')
            k1, k2 = st.columns(2)
            k1.metric("中位回撤", f"{mc.get('drawdown_p50', 0):.2f}%")
            k2.metric("95% 分位回撤", f"{mc.get('drawdown_p95', 0):.2f}%")
            st.caption(
                f"实测路径回撤 {mc.get('observed_realized_drawdown', 0):.2f}%，"
                f"重排中有 {mc.get('worse_than_observed_pct', 0):.0f}% 的路径比它更深。"
                f"实盘应按 95% 分位（{mc.get('drawdown_p95', 0):.2f}%）准备承受能力。"
            )
        with g2:
            rets = mc.get("return_samples") or []
            if rets:
                fig_ret = go.Figure()
                fig_ret.add_trace(go.Histogram(
                    x=rets, nbinsx=40, marker_color="rgba(90,216,166,0.75)", name="收益分布",
                ))
                fig_ret.add_vline(x=0, line_dash="dash", line_color="#B0B0B0")
                fig_ret.update_layout(
                    height=300, margin=dict(l=10, r=10, t=30, b=10),
                    xaxis_title="已实现盈亏收益率（%）", yaxis_title="频次", showlegend=False,
                )
                st.plotly_chart(fig_ret, width='stretch')
            r1, r2 = st.columns(2)
            r1.metric("收益 90% 区间",
                      f"{mc.get('final_return_p5', 0):.1f}% ~ {mc.get('final_return_p95', 0):.1f}%")
            r2.metric("亏损概率", f"{mc.get('loss_probability', 0):.1f}%",
                      help="自助重采样下有放回抽取同笔数交易，收益为负的比例")

        st.caption(
            f"以上分布基于已实现盈亏（合计 {mc.get('realized_pnl_total', 0):,.0f} 元），"
            f"与组合盯市口径的总收益 {report.oos_total_return:.2f}% 不可直接比较 —— "
            f"后者含持仓浮盈浮亏。组合盯市回撤为 {mc.get('observed_drawdown', 0):.2f}%。"
        )

    # ---------- 警告与导出 ----------
    for w in report.warnings:
        st.warning(w)

    with st.expander("📄 文字版报告（可复制/下载）"):
        summary = report.summary()
        st.code(summary, language="text")
        st.download_button(
            "⬇️ 下载 Markdown",
            data=f"# 样本外验证报告\n\n```\n{summary}\n```\n",
            file_name=f"walk_forward_{meta.get('strategy', 'strategy')}.md",
            mime="text/markdown",
            key="wf_download",
        )


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
    stock_df = cached_instrument_list()
    stocks_with_data = cached_get_stocks_with_data(min_days=60)
    if stock_df.empty:
        st.warning("暂无标的数据")
        return

    st.subheader("📊 多策略权益曲线对比")

    strategies = list_strategies()
    strategy_names = [s["name"] for s in strategies]
    strategy_descs = {s["name"]: s["desc"] for s in strategies}

    selected_strategies = st.multiselect(
        "选择要对比的策略",
        strategy_names,
        default=strategy_names,
        format_func=lambda x: strategy_label(
                x, strategy_descs[x],
                getattr(STRATEGY_REGISTRY[x], "style", "综合"),
            ),
    )

    if not selected_strategies:
        st.warning("请至少选择一个策略")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        ms_name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}
        ms_type_map = dict(zip(stock_df["ts_code"], stock_df["type"])) if not stock_df.empty else {}
        valid_ms_codes = [c for c in stocks_with_data if c in ms_name_map]
        if valid_ms_codes:
            selected_ms = st.selectbox(
                "选择对比标的 (支持搜索)",
                valid_ms_codes,
                format_func=lambda c: f"[{ms_type_map.get(c, '标的')}] {c} {ms_name_map.get(c, '')}",
                key="ms_code_select"
            )
            codes_input = selected_ms
            st.caption(f"🏷️ 当前标的: **{codes_input} {ms_name_map.get(codes_input, '')}**")
        else:
            codes_input = st.text_input("股票代码", placeholder="000001.SZ", key="ms_code")
            matched_name = ms_name_map.get(codes_input.strip(), "")
            if matched_name:
                st.caption(f"🏷️ 当前标的: **{codes_input.strip()} {matched_name}**")
            else:
                st.caption(f"📊 数据库有数据标的: {len(stocks_with_data)} 只")
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

            stock_df = get_instrument_list()
            name_map = dict(zip(stock_df["ts_code"], stock_df["name"]))

            # 构建矩阵数据（先一次性预加载全部自选股数据与指标，消除 N×M 重复 I/O 与计算）
            matrix_data = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            status_text.text("正在预热加载行情与技术指标...")
            preloaded_matrix = preload_backtest_data(watchlist_codes, ms_start, ms_end)
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
                        execution_mode="next_open",
                        preloaded_data=preloaded_matrix,
                    )
                    result = bt.run(save=False)
                    ret = result.total_return if result.equity_curve else None
                    matrix_data.append({
                        "strategy": format_strategy_cn(sname),
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

        # 原有单只股票对比模式（单次预热加载行情，消除18个策略重复读取SQLite和重算指标）
        if not codes_input:
            st.error("请输入股票代码")
            return

        universe = [codes_input.strip()]
        start_str = ms_start
        end_str = ms_end

        curves = []
        names = []
        strat_results = []

        with st.spinner("正在运行多策略回测..."):
            preloaded_single = preload_backtest_data(universe, start_str, end_str)
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
                    execution_mode="next_open",
                    preloaded_data=preloaded_single,
                )
                result = bt.run(save=False)
                if result.equity_curve:
                    display_name = f"{format_strategy_cn(sname)} ({sname})"
                    curves.append(result.equity_curve)
                    names.append(display_name)
                    strat_results.append((display_name, result))

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

        # 叠加沪深300基准（归一化到与策略同一起点）
        if show_benchmark:
            benchmark_curve = _get_benchmark_curve(start_str, end_str)
            if benchmark_curve:
                bm_df = pd.DataFrame(benchmark_curve)
                if not bm_df.empty and len(bm_df) >= 2:
                    bm_init = float(bm_df["equity"].iloc[0])
                    bm_normalized = bm_df["equity"] / bm_init * DEFAULT_CAPITAL
                    total_bm_ret = (float(bm_df["equity"].iloc[-1]) / bm_init - 1) * 100
                    fig.add_trace(go.Scatter(
                        x=bm_df["date"], y=bm_normalized,
                        mode="lines", name=f"沪深300基准 ({total_bm_ret:+.1f}%)",
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

        # 指标对比表（修正原本交易次数写死为 len(curves) 的 Bug，补充真实夏普与胜率并按总收益降序排列）
        st.subheader("📋 指标对比")
        strat_results.sort(key=lambda item: item[1].total_return, reverse=True)
        rows = []
        for name, res in strat_results:
            rows.append({
                "策略": name,
                "总收益%": f"{res.total_return:+.2f}",
                "年化收益%": f"{res.annual_return:+.2f}",
                "夏普比率": f"{res.sharpe_ratio:.2f}",
                "最大回撤%": f"{res.max_drawdown:.2f}",
                "胜率%": f"{res.win_rate:.1f}",
                "交易次数": int(res.trade_count),
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

    # 指标卡片 (第一行：核心收益与风险)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总收益", f"{result.total_return:.2f}%")
    c2.metric("年化收益", f"{result.annual_return:.2f}%")
    c3.metric("最大回撤", f"{result.max_drawdown:.2f}%")
    c4.metric("夏普比率", f"{result.sharpe_ratio:.2f}")
    c5.metric("卡玛比率", f"{result.calmar_ratio:.2f}")
    c6.metric("综合胜率", f"{result.win_rate:.1f}%")

    # 第二行指标：高胜率与盈亏比双轮驱动核心看板 (页面清晰明了)
    st.markdown("##### 🎯 胜率与盈亏比双轮驱动核心看板")
    pf = getattr(result, "profit_factor", 1.0)
    avg_w = getattr(result, "avg_win", 0.0)
    avg_l = getattr(result, "avg_loss", 0.0)
    be_cnt = getattr(result, "break_even_count", 0)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("盈亏比 (Profit Factor)", f"{pf:.2f}", help="总盈利额 / 总亏损额，> 1.5 表明盈利质量显著超越试错成本")
    m2.metric("平均单笔盈利", f"+¥{avg_w:,.1f}")
    m3.metric("平均单笔亏损", f"-¥{avg_l:,.1f}")
    m4.metric("保本出场单数", f"{be_cnt} 笔", help="浮盈达标后回撤至成本线保本平仓的笔数，成功守护本金")
    m5.metric("平仓/总交易", f"{getattr(result, 'sell_count', 0)} / {result.trade_count} 笔")
    if hasattr(result, 'alpha') and result.alpha != 0:
        m6.metric("Alpha 超额", f"{result.alpha:.2f}%")
    else:
        m6.metric("初始资金", f"¥{result.initial_capital:,.0f}")

    # 自然语言结论
    _show_nlp_summary(result)

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

    # 回测买卖点 K 线复盘图 + 交易记录
    if result.trades:
        stock_df = cached_instrument_list()
        name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}

        # 提取所有发生交易的股票，在K线图上直接标注红买/绿卖箭头
        traded_codes = list(dict.fromkeys(t.ts_code for t in result.trades))
        with st.expander("🕯️ 回测买卖点 K 线复盘图（红「买」绿「卖」精确标注）", expanded=True):
            sel_code = st.selectbox(
                "选择标的查看买卖点K线",
                traded_codes,
                format_func=lambda c: f"{c} {name_map.get(c, '')}".strip(),
                key="bt_kline_review_code",
            ) if len(traded_codes) > 1 else traded_codes[0]

            k_df = get_daily(sel_code, result.start_date, result.end_date)
            if not k_df.empty:
                from app.data_viewer import create_candlestick_chart
                k_df = apply_indicators(k_df, ["ma", "vol_ma", "macd"])
                buy_dates = [t.trade_date for t in result.trades if t.ts_code == sel_code and t.direction == "BUY"]
                sell_dates = [t.trade_date for t in result.trades if t.ts_code == sel_code and t.direction == "SELL"]
                k_fig = create_candlestick_chart(
                    k_df, sel_code, name_map.get(sel_code, sel_code),
                    show_volume=True, show_macd=True, show_boll=False,
                    buy_dates=buy_dates, sell_dates=sell_dates,
                    height=460,
                )
                st.plotly_chart(k_fig, width="stretch")

        st.subheader("📋 交易记录")
        trade_data = []
        for t in result.trades:
            trade_data.append({
                "日期": t.trade_date,
                "股票": format_stock_cn(t.ts_code, name_map),
                "方向": format_direction_cn(t.direction),
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
策略: {format_strategy_cn(result.strategy)} ({result.strategy})
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

    # 结构化月度与年度日历明细矩阵表 (页面清晰明了)
    cal_rows = []
    for y in years:
        r_item = {"年份": str(y)}
        for m in range(1, 13):
            val = heat_data[years.index(y)][m - 1]
            r_item[f"{m}月"] = f"{val:+.1f}%" if pd.notna(val) else "-"
        y_slice = eq_df[eq_df["year"] == y]["daily_return"]
        y_ret = ((1 + y_slice).prod() - 1) * 100 if not y_slice.empty else 0.0
        r_item["全年合计"] = f"{y_ret:+.1f}%"
        cal_rows.append(r_item)

    st.markdown("**📅 月度与年度收益日历明细表**")
    st.dataframe(pd.DataFrame(cal_rows), hide_index=True, width="stretch")


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
    """获取沪深300基准收益曲线（优先本地数据库，失败则尝试外网拉取）"""
    try:
        from data.storage import get_index_daily_range
        # 优先读取本地数据库沪深300指数 (000300.SH 或 399300.SZ)
        for code in ["000300.SH", "399300.SZ", "000001.SH"]:
            df_local = get_index_daily_range(code, start_date, end_date)
            if not df_local.empty and len(df_local) >= 2:
                first_close = float(df_local["close"].iloc[0])
                if first_close > 0:
                    return [
                        {"date": str(row["trade_date"]), "equity": float(row["close"]) / first_close * 1000}
                        for _, row in df_local.iterrows()
                    ]

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
        first_close = float(df["close"].iloc[0])
        if first_close <= 0:
            return None
        curve = []
        for _, row in df.iterrows():
            curve.append({
                "date": row["trade_date"],
                "equity": float(row["close"]) / first_close * 1000,
            })
        return curve
    except Exception:
        return None


def _show_history():
    """历史回测记录 + 多记录对比"""
    results_df = get_backtest_results(limit=50)
    if results_df.empty:
        st.info("暂无回测记录")
        return

    st.subheader("📋 历史回测记录")

    # 对比模式
    st.markdown("**勾选要对比的记录（最多 5 条）**")
    col_chk, col_btn = st.columns([4, 1])
    with col_chk:
        # 用 checkbox 选 ID
        selected_ids = []
        for idx, row in results_df.head(20).iterrows():
            label = (f"#{row['id']} {format_strategy_cn(row['strategy'])} ({row['strategy']}) | "
                     f"{row['start_date']}~{row['end_date']} | "
                     f"收益 {row['total_return']:.2f}%")
            if st.checkbox(label, key=f"hist_chk_{row['id']}"):
                selected_ids.append(row["id"])

    with col_btn:
        compare_clicked = st.button("📊 对比选中", type="primary",
                                     disabled=len(selected_ids) < 2,
                                     width="stretch")

    if compare_clicked and len(selected_ids) >= 2:
        _show_comparison(selected_ids[:5])
        st.markdown("---")

    # 详细记录展示
    for _, row in results_df.head(20).iterrows():
        with st.expander(
            f"#{row['id']} {format_strategy_cn(row['strategy'])} ({row['strategy']}) | {row['start_date']}~{row['end_date']} | "
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
                stock_df = cached_instrument_list()
                name_map = dict(zip(stock_df["ts_code"], stock_df["name"])) if not stock_df.empty else {}
                disp_trades = trades_df[["trade_date", "ts_code", "direction",
                                        "price", "volume", "pnl", "holding_days"]].copy()
                disp_trades["ts_code"] = disp_trades["ts_code"].apply(lambda c: format_stock_cn(c, name_map))
                disp_trades["direction"] = disp_trades["direction"].apply(format_direction_cn)
                disp_trades = disp_trades.rename(columns={
                    "trade_date": "日期", "ts_code": "股票", "direction": "方向",
                    "price": "价格", "volume": "数量", "pnl": "盈亏", "holding_days": "持仓天数",
                })
                chinese_dataframe(disp_trades)


def _show_comparison(backtest_ids: list):
    """并排展示多条回测结果的权益曲线和指标"""
    st.subheader("📊 回测结果对比")

    results = []
    for bt_id in backtest_ids:
        r = get_backtest_result_by_id(bt_id)
        if r and r.get("equity_curve"):
            results.append(r)

    if len(results) < 2:
        st.warning("有效记录不足 2 条，无法对比")
        return

    # 权益曲线叠加图
    fig = go.Figure()
    for r in results:
        eq = r["equity_curve"]
        if not eq:
            continue
        df_eq = pd.DataFrame(eq)
        total_ret = (df_eq["equity"].iloc[-1] / df_eq["equity"].iloc[0] - 1) * 100
        label = f"#{r['id']} {format_strategy_cn(r['strategy'])} ({total_ret:+.1f}%)"
        fig.add_trace(go.Scatter(
            x=df_eq["date"], y=df_eq["equity"],
            mode="lines", name=label, line=dict(width=2),
        ))

    fig.update_layout(
        title="权益曲线对比",
        template="plotly_dark", height=450,
        xaxis_title="日期", yaxis_title="权益(元)",
        xaxis=dict(type="category", nticks=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, width="stretch")

    # 指标对比表格
    compare_rows = []
    for r in results:
        compare_rows.append({
            "ID": r["id"],
            "策略": format_strategy_cn(r["strategy"]),
            "日期范围": f"{r['start_date']}~{r['end_date']}",
            "总收益%": float(r.get("total_return", 0) or 0),
            "年化%": float(r.get("annual_return", 0) or 0),
            "夏普": float(r.get("sharpe_ratio", 0) or 0),
            "最大回撤%": float(r.get("max_drawdown", 0) or 0),
            "胜率%": float(r.get("win_rate", 0) or 0),
            "交易次数": int(r.get("trade_count", 0) or 0),
        })
    df_compare = pd.DataFrame(compare_rows)
    styled_compare = (
        df_compare.style.highlight_max(
            subset=["总收益%", "年化%", "夏普", "胜率%"], color="#90EE90"
        ).highlight_min(subset=["最大回撤%"], color="#FFB3B3")
        .format({
            "总收益%": "{:+.2f}%",
            "年化%": "{:+.2f}%",
            "夏普": "{:.2f}",
            "最大回撤%": "{:.2f}%",
            "胜率%": "{:.1f}%",
        })
    )
    st.dataframe(styled_compare, hide_index=True, width="stretch")

    # 导出对比结果
    csv_data = df_compare.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 导出对比结果 CSV",
        data=csv_data,
        file_name=f"回测对比_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
