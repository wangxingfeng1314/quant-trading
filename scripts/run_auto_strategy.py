"""
一键运行双舱位核心-卫星组合回测与策略路由实时信号扫描
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

project_root = Path(__file__).parent.parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.config import DEFAULT_CAPITAL
from strategies.momentum_rotation import ROTATION_ETF_PRESETS
from data.storage import get_watchlist, get_instrument_list, get_stocks_with_data
from engine.backtester import run_dual_sleeve_backtest
from engine.scanner import scan_signals


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    default_end = datetime.now().strftime("%Y%m%d")
    default_start = (datetime.now() - timedelta(days=365 * 2 + 180)).strftime("%Y%m%d")

    parser = argparse.ArgumentParser(description="运行双舱位核心-卫星组合回测与今日信号扫描")
    parser.add_argument("--start", default=default_start, help="回测起始日 YYYYMMDD")
    parser.add_argument("--end", default=default_end, help="回测结束日 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=float(DEFAULT_CAPITAL), help="初始资金")
    parser.add_argument("--stock-weight", type=float, default=0.60, help="个股滚动核心舱权重 (0.1~0.9)")
    parser.add_argument("--etf-sizing", default="atr_risk", choices=["atr_risk", "score"], help="ETF 轮动仓位模型")
    parser.add_argument("--no-save", action="store_true", help="仅运行扫描不入库信号")
    args = parser.parse_args()

    wl = get_watchlist()
    wl_codes = wl["ts_code"].tolist() if wl is not None and not wl.empty else []
    grp_map = (
        dict(zip(wl["ts_code"], wl["group_name"].fillna("")))
        if (wl is not None and not wl.empty and "group_name" in wl.columns)
        else {}
    )
    stock_codes = [
        c for c in wl_codes
        if str(grp_map.get(c, "")) != "ETF轮动池"
        and not str(c).upper().startswith(("51", "56", "58", "15", "16"))
    ]
    ready_set = set(get_stocks_with_data(min_days=60))
    etf_codes = [c for c, _ in ROTATION_ETF_PRESETS if c in ready_set]
    inst = get_instrument_list()
    name_map = dict(zip(inst["ts_code"], inst["name"])) if not inst.empty else {}

    print("=" * 60)
    print("🚀 运行双舱位核心-卫星组合回测与策略路由实时信号扫描")
    print("=" * 60)
    print(f"个股核心池 ({len(stock_codes)} 只) | ETF 轮动池 ({len(etf_codes)} 只) | 区间 {args.start} ~ {args.end}")

    if stock_codes or etf_codes:
        ds_out = run_dual_sleeve_backtest(
            stock_universe=stock_codes,
            etf_universe=etf_codes,
            start_date=args.start,
            end_date=args.end,
            stock_weight=float(args.stock_weight),
            rebalance_months=12,
            train_lookback_years=3,
            etf_rebalance_days=10,
            etf_top_k=2,
            etf_position_sizing=args.etf_sizing,
            initial_capital=float(args.capital),
            execution_mode="next_open",
            risk_manager="auto",
            max_active_positions=5,
            position_sizing="atr_risk",
            market_regime_filter=True,
            dual_index_regime=True,
            bear_position_scale=0.35,
            max_open_gap_pct=3.0,
            cash_yield_annual=1.8,
        )
        res = ds_out["result"]
        s_res = ds_out["stock_result"]
        e_res = ds_out["etf_result"]
        print("\n==================== 双舱位组合回测成果 ====================")
        print(f"配置模型:     个股滚动({ds_out['weights']['stock']*100:.0f}%) + ETF({ds_out['weights']['etf']*100:.0f}%, {args.etf_sizing})")
        print(f"回测区间:     {res.start_date} ~ {res.end_date}")
        print(f"初始资金:     ¥{res.initial_capital:,.2f}")
        print(f"最终资产:     ¥{res.final_capital:,.2f}")
        print(f"累计总收益率: {res.total_return:+.2f}% | 年化收益: {res.annual_return:+.2f}%")
        print(f"最大回撤:     {res.max_drawdown:.2f}% | 夏普: {res.sharpe_ratio:.2f} | 卡玛: {res.calmar_ratio:.2f}")
        print(f"子舱拆分:     个股 {s_res.total_return:+.2f}% (DD {s_res.max_drawdown:.2f}%) | ETF {e_res.total_return:+.2f}% (DD {e_res.max_drawdown:.2f}%)")
        print(f"总交易笔数:   {res.trade_count} 笔 (平仓 {res.sell_count} 笔 | 胜率 {res.win_rate:.1f}%)")
    else:
        print("⚠️ 当前数据库暂无自选股与轮动 ETF 历史数据，跳过组合回测。")

    scan_universe = list(dict.fromkeys(stock_codes + etf_codes))
    if scan_universe:
        print("\n正在扫描最新实时交易信号 (双指数择时 + 自选股策略路由 + 防鞭尸过滤)...", flush=True)
        sigs = scan_signals(
            universe=scan_universe,
            save=not args.no_save,
            respect_market_regime=True,
            respect_watchlist_strategy=True,
            anti_whipsaw=True,
        )
        print("\n==================== 最新交易信号推荐 ====================")
        if sigs:
            print(f"共生成 {len(sigs)} 条过滤后信号:\n")
            for idx, s in enumerate(sigs, 1):
                stock_name = name_map.get(s.ts_code, "")
                direction_tag = "🔴 买入 (BUY)" if s.direction == "BUY" else "🟢 卖出 (SELL)"
                print(f"[{idx}] {direction_tag} {s.ts_code} {stock_name}")
                print(f"    - 策略来源: {s.strategy} | 参考价: ¥{s.price_ref:.3f} | 评分: {s.score:.2f}")
                print(f"    - 触发原因: {s.reason}\n")
        else:
            print("当前时点无符合择时与策略路由条件的开/平仓信号。")

    print("=" * 60)
    print("✅ 双舱位策略运算与信号扫描完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
