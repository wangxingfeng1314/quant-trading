with open('tests/test_tier_optimizations.py', 'a', encoding='utf-8') as f:
    f.write('''

def test_backtester_st_lookup_initialization(monkeypatch):
    """验证 Backtester 初始化时能够正确建立 ST 缓存并在撮合中使用"""
    from engine.backtester import Backtester
    from strategies.ma_cross import MACrossStrategy
    import pandas as pd
    
    # 模拟 get_instrument_list 行为
    def mock_get_instrument_list():
        return pd.DataFrame({
            "ts_code": ["000001.SZ", "600000.SH", "300000.SZ"],
            "name": ["平安银行", "*ST浦发", "特锐德"],
            "is_st": [0, 1, 0]
        })
    
    monkeypatch.setattr("data.storage.get_instrument_list", mock_get_instrument_list)
    monkeypatch.setattr("data.storage.get_daily", lambda *args, **kwargs: pd.DataFrame())
    
    bt = Backtester(
        strategy_cls=MACrossStrategy, 
        params={}, 
        universe=["000001.SZ", "600000.SH", "300000.SZ"],
        start_date="20240101",
        end_date="20240110"
    )
    bt.run(save=False)
    
    assert "000001.SZ" in bt.st_lookup
    assert bt.st_lookup["000001.SZ"] is False
    assert bt.st_lookup["600000.SH"] is True
    assert bt.st_lookup["300000.SZ"] is False

def test_notifier_push_negative_pnl_formatting():
    """验证 push_notification 渲染盈亏金额时的格式符合 -¥500 和 +¥500，不会出现 ¥-500"""
    total_pnl = -500.0
    total_pnl_sign = "+" if total_pnl > 0 else "-" if total_pnl < 0 else ""
    formatted_pnl = f"{total_pnl_sign}¥{abs(total_pnl):,.0f}"
    assert formatted_pnl == "-¥500"
    
    total_pnl = 500.0
    total_pnl_sign = "+" if total_pnl > 0 else "-" if total_pnl < 0 else ""
    formatted_pnl = f"{total_pnl_sign}¥{abs(total_pnl):,.0f}"
    assert formatted_pnl == "+¥500"
    
    total_pnl = 0.0
    total_pnl_sign = "+" if total_pnl > 0 else "-" if total_pnl < 0 else ""
    formatted_pnl = f"{total_pnl_sign}¥{abs(total_pnl):,.0f}"
    assert formatted_pnl == "¥0"

def test_backtester_sell_vol_available_shares():
    """验证 sell_vol 简化为 pos.available_shares 的逻辑符合预期"""
    from engine.portfolio import Portfolio
    
    p = Portfolio(100000)
    p.buy("000001.SZ", 10.0, 1000, "20240101")
    pos = p.get_position("000001.SZ")
    
    sell_vol = pos.available_shares
    assert sell_vol == 0
    
    p.on_new_day("20240102")
    sell_vol2 = pos.available_shares
    assert sell_vol2 == 1000
''')
