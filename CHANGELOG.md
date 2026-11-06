# Changelog

## [v0.4.2] - 2026-11-06

### 🎯 自选股 AI 策略智能推荐与性格诊断 (Portfolio & Recommender)
- **AI 策略智能推荐引擎 (`services/strategy_recommender.py`)** — 基于标的历史行情数据与四大冠军策略（短线KDJ、震荡布林、顺势双均线、综合多因子），采用实盘因果时序快速回测，综合考量累计收益、胜率与最大回撤，自动为每只标的量身匹配最佳策略模型。
- **持仓管理「推荐策略」一览** — 「持仓管理」自选股列表正式增加 **推荐策略** 核心列，直观展示标的最佳匹配策略、历史胜率与收益标签（如 `⚡ KDJ企稳 (胜率80.0%/+2.3%)`、`📈 双均线趋势 (胜率55.6%/+8.9%)`）。
- **添加自选股实时诊断与回测匹配** — 在搜索并选择股票时，系统实时输出标的性格与 AI 策略适配建议卡片；添加时自动下载历史行情并执行深度回测校准最佳策略，写入自选股推荐。
- **数据库平滑迁移 (v4)** — `watchlist` 表自动迁移并增加 `recommended_strategy` 字段，支持界面一键「🎯 重新评估策略」一键批量刷新。
- **数据补全** — 彻底补齐士兰微 (`600460.SH`) 近 5 年全量日线数据（1296 条），智能评测推荐采用双均线顺势策略。

### 🧪 自动化测试套件扩充
- 新增 `tests/test_strategy_recommender.py` 智能推荐单元测试，更新数据库结构对齐检查，全项目测试用例扩充至 **201 项**，100% 顺利通过。

---

## [v0.4.1] - 2026-11-05

### 🧩 策略库精简与四大风格冠军策略调优 (Strategies & Backtester)
- **策略体系极致精选** — 对 18 个历史策略在 A 股次日开盘 (`next_open`) 真实撮合环境下进行全量回测评估，淘汰 14 个低胜率或高回撤策略，在短线、中长线、震荡、综合四大风格中各保留 1 个实战表现最佳的冠军策略：
  - ⚡ **短线冠军**：`kdj_cross.py`（KDJ超卖企稳金叉与极值高抛），胜率 59.7%，收益 +25.9%，回撤 15.2%
  - 📈 **中长线冠军**：`ma_cross.py`（双均线顺势右侧趋势追踪），胜率 35.3%，收益 +40.8% (年化 +9.8%)，回撤大幅压降至 13.9%
  - ↔️ **震荡冠军**：`bollinger_reversal.py`（布林带触轨反转与高抛止盈），胜率高达 60.3%，收益扭亏为盈达到 +8.0%
  - 🧩 **综合冠军**：`signal_combo.py`（多因子信号高置信共振），收益 +47.9%，年化 +11.25%，夏普比率 0.60
- **胜率增强与假突破拦截** — 引入右侧斜率过滤、极值主动止盈防利润回吐、大阳线透支过滤与多维共振打分门槛，显著压降假信号。

### 🌐 页面导航与 URL 双向同步 (Navigation & Routing)
- **URL 参数双向绑定** — 侧边栏单选导航与浏览器 URL query parameters (`?page=...`) 实时双向同步；F5 刷新、直接访问链接均能精准定位到对应页面，不再重置回首页。
- **友好别名与规范化** — 支持中英文及常用别名自动规范化（如 `?page=bt` 自动解析为 `backtest`，`?page=选股` 自动解析为 `screener`）。
- **浏览器前进/后退支持** — 监听 URL 变化自动同步切页，全面兼容浏览器历史操作。

### 🎨 布局体验与视觉间距深度修复 (UI & Layout)
- **多股票搜索交互升级** — 回测中心与信号中心的多股票筛选由手动文本输入升级为支持实时模糊搜索的多选框控件 (`st.multiselect`)，并提供快捷填充按钮。
- **消除主页面顶部空白 div** — 彻底定位并移除了由无感组件容器与 `st.empty()` 导致的主页面标题上方空白 div 块，收拢 Streamlit 默认的 `6rem` 顶部留白至紧凑的 `1.8rem`。
- **侧边栏顶格贴合** — 将样式注入移至侧边栏末尾，收拢侧边栏 header/content 边距并消除 `h1` 默认外边距，侧边栏标题直接紧贴顶部，无任何空白区域。

### 🧪 自动化测试套件扩充
- 新增 `tests/test_url_navigation.py` 自动化测试套件，全系统测试用例数扩充至 **196 项**，100% 全部通过。

---

## [v0.4.0] - 2026-09-16

### 🛡️ 因果律时序与鲁棒性加固 (Core & Strategies)
- **突破策略因果律隔离** — 修复海龟突破 (`turtle.py`) 与唐奇安通道 (`donchian_breakout.py`) 时序判定错位，引入隔离的昨日通道基准 `prev_high_n` / `prev_upper`，彻底消除连阳持续虚假触发买入信号的重大隐患。
- **背离策略基准时序修正** — 修复 MACD / RSI 背离策略因统计窗口包含今日导致底背离判定恒为假的逻辑锁死缺陷，前序窗口严格隔离前推。
- **布林带收口容错** — 布林带收口策略 (`boll_squeeze.py`) 引入昨日带宽状态容错，消除突破当日带宽暴涨导致收口判定失效的伪拒绝。
- **数据断层防护 (Tier 2 架构)** — `DataService` 与 `init_data.py` 全面接入 `check_split_dividend_anomaly`，在增量同步时自动识别除权除息导致的复权基准重置，并自动执行陈旧缓存清空与全量重取覆写。
- **对象语义规范** — `Position` 数据类增加 `__bool__` 方法（映射 `not self.is_empty`），彻底解决空仓对象被误判为“有持仓”的 Python dataclass 默认布尔缺陷。
- **关键依赖防崩溃** — 补全 `multi_factor.py` 与 `signal_combo.py` 缺失的 `import pandas as pd`，杜绝真实日线数据包含 `pct_chg` 时触发 `NameError`。
- **置信度基线归一化** — 统一优化均线交叉、均线多头、KDJ金叉死叉、RSI超买超卖策略在微小穿越时的置信度基线（提升至 0.50~0.60），防止被下游排序和阈值误过滤。

### 💼 实盘与风控引擎增强 (Engine & Services)
- **统一出场风控管理器** — 新增 `engine/risk_manager.py`，支持固定比例硬止损、动态跟踪止盈（最高浮盈激活 + 高点回撤锁定）、最大持仓天数自动退出。
- **全链路决策上下文快照 (Tier 3 架构)** — `Signal`、`Trade`、`position` 表与风控系统全链路集成 `context_snapshot`，完整记录买卖当时的市场状态与决策依据。
- **多策略同标的信号仲裁** — `resolve_signal_conflicts` 实现同向共振增强增信与多空冲突净额博弈（Net Score），消除相反信号互搏。
- **回测引擎增强** — 支持 `next_open`（次日开盘价，防未来函数）与 `current_close`（当日收盘价）双撮合模式；支持最大持仓只数动态控制 (`max_active_positions`)；修复权益曲线末日重复记录与指标扭曲；实现指数日线本地离线检索。

### 🖥️ 交互与实盘交付优化 (UI & UX)
- **券商实盘批量委托单生成器** — 信号中心新增 Broker-Ready Order Sheet，支持按资金上限与加折价自动算手，智能匹配当前实际持仓卖出股数，一键导出通达信/同花顺/通用券商格式。
- **持仓与自选列表列结构规范** — 模拟持仓与自选股列表统一格式化为规范复合“股票”列，去除冗余的“代码”与“名称”列。
- **选股器与数据浏览单位修正** — 彻底修正成交量显示单位为规范“万手”。
- **表单级封装防跳顶** — 移除自选股与分组操作采用表单封装，消除多选勾选时的页面重载与视口跳动。

### 🧪 测试体系完善
- 单元测试与回归套件扩充至 **191 项**，覆盖全部模型、策略、存储、风控与UI缓存，100% 通过。

---

## [v0.3.1] - 2026-08-11

### 性能优化
- `engine/backtester.py` — 回测引擎零拷贝：`df.iloc[:idx+1]` 视图替代 `copy()`，消除 O(n²) 内存拷贝
- `data/storage.py` — 新增 `save_signals_batch()` + `executemany` 批量写入，消除 N+1 写入
- `data/storage.py` — `threading.local()` SQLite 连接池，线程级连接复用
- `engine/scanner.py` — `ThreadPoolExecutor` 并行信号扫描（≥50 只股票自动并行）

### 架构优化
- `core/exceptions.py`（新）— 自定义异常体系：`QuantError` 基类 + `DataFetchError`/`BacktestError`/`ValidationError`/`StorageError`
- `data/fetcher_base.py`（新）— `DataSource(ABC)` 抽象基类 + `DAILY_COLUMNS` 常量
- `data/fetcher_circuit.py`（新）— 从 fetcher.py 抽取 `CircuitBreaker` 熔断 + `TokenBucket` 限流
- `services/`（新包）— `DataService` / `BacktestService` / `SignalService` + `validators.py` 服务层
- `data/storage.py` — `PRAGMA user_version` 版本化数据库迁移系统

### 安全优化
- `notifier/push.py` — Webhook URL 脱敏 `_mask_url()` + `_safe_log()`，防密钥泄露
- `services/validators.py`（新）— 股票代码/日期/资金输入校验
- `app/main.py` — Streamlit 登录认证（`APP_AUTH_ENABLED`）+ 数据库恢复二次确认

### 可维护性优化
- `core/config.py` — 集中化配置（扫描/回测/熔断/认证等散落魔法数字）
- `app/main.py` — 数据库恢复危险操作增加 checkbox 二次确认

### 用户体验优化
- `app/backtest.py` — 回测结果对比（多结果叠加曲线 + 指标对比表 + CSV 导出）
- `app/signal.py` / `app/portfolio.py` — CSV 导出（信号扫描/历史信号/持仓清单）
- `app/theme.py` — 响应式 CSS 移动端适配

### 测试
- 单元测试调整至 **89 个**，全部通过，零回归

---

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
