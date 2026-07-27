# Changelog

## [v0.3.0] - 2026-07-15

### 自选股中心化重构
- 系统从"全市场扫描"改为"自选股中心" — 所有功能页面只操作有数据的股票
- `storage.py` — 新增 `get_stocks_with_data(min_days)` 辅助函数
- `init_data.py` — 新增 `--watchlist` 参数（初始下载/增量更新只针对自选股）
- `run_update()` — 新增 `watchlist=True` 参数 + `progress_callback` 支持
- `update_data.bat` / `scheduler/__init__.py` — 定时任务全加 `--watchlist`

### 策略体系扩展（11个策略）
- **新增4个策略**：唐奇安通道突破、量价突破、双底形态识别、多因子综合评分
- **分类体系**：趋势跟踪(4) + 反转交易(4) + 动量(2) + 组合(1)
- 策略参数模板保存/加载 + 标记当前参数
- 组合信号合成（多策略共识分析）

### 页面功能增强
- 信号中心 — 扫描范围改为"有数据的股票"/"自选股(快)" + 历史信号 + 信号验证
- 回测中心 — 参数网格搜索(热力图) / 多策略对比 / 绩效归因(月度热力图/滚动夏普)
- 持仓管理 — 自选股分组(长线池/短线池) + 模拟持仓持久化 + 信号自动跟单
- 首页看板 — 数据健康度 + 每日复盘报告生成+导出

### Bug修复
- `scripts/update_data.bat` — LF→CRLF换行符 + 中文路径改用 `%USERPROFILE%`
- `app/backtest.py` — `st.number_input` 去掉 `min_value=0.0` 限制（支持 sell_threshold=-3.0）
- `core/models.py` + `engine/backtester.py` — `BacktestResult` 新增 `calmar_ratio` 字段
- `app/backtest.py` — `_draw_monthly_heatmap` 中 `int(row["month"])` 修复 numpy.float64 索引问题
- `data/storage.py` — `save_signal` 改为 `INSERT OR REPLACE` 修复 UNIQUE 约束冲突

---

## [v0.2.0] - 2026-07-01

### 初始版本发布
- Streamlit 7页面架构（首页看板/数据浏览/回测中心/选股筛选/板块热力/信号中心/持仓管理）
- 自研 pandas 回测引擎 + 网格搜索
- 多数据源级联：AKShare → Tushare → Baostock
- 7个初始策略：双均线交叉/MACD背离/海龟突破/RSI超买超卖/布林带反转/KDJ金叉死叉/均线多头排列
- A股交易费用模型（佣金/印花税/过户费/滑点/涨跌停/T+1）
- SQLite 数据库 + Plotly 图表
- 数据源熔断保护 + 自动重试
- Windows 定时任务（工作日17:00自动增量更新）
- Server酱/PushPlus 消息推送
