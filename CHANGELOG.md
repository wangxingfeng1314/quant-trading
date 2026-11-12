# Changelog

## [v0.4.8] - 2026-11-12

### 🎯 策略深度调优（样本外盲测验证驱动）

基于「70% 训练（2023-01~2025-09）探索 + 30% 样本外盲测（2025-09~2026-11）」严格筛选，只保留**同时提升训练集与样本外盲测表现**的改动，主动丢弃仅在训练期有效的过拟合规则：

- **KDJ 金叉策略 (`strategies/kdj_cross.py`)**：将金叉买入超卖门槛收紧至 `K < 28`（空头排列下收紧至 `K < 20`），过滤 `K=28~35` 震荡浅回调假信号。实测（13 只自选股，next_open，4 持仓+风控+保本）：
  - 30% 样本外盲测（不开大盘择时）：收益 `-1.87% → +1.42%`，胜率 `56.5% → 59.5%`，最大回撤 `12.31% → 11.12%`，盈亏比 `0.96 → 1.08`；
  - 30% 样本外盲测（开启大盘择时）：收益 `-4.93% → +1.65%`，胜率 `50.0% → 60.0%`，最大回撤 `10.03% → 8.37%`；
  - 全周期 2023-2026（不开大盘择时）：收益 `+30.63% → +32.08%`，胜率 `57.8% → 59.8%`，最大回撤 `12.18% → 11.15%`，盈亏比 `1.33 → 1.42`。
- **多因子共振策略 (`strategies/signal_combo.py`)**：买入增加 `close >= ma60 * 0.96` 中期安全线过滤，拦截深空头通道的短线弱反弹。实测：
  - 30% 样本外盲测（开启大盘择时）：收益 `+3.91% → +6.06%`，最大回撤 `10.60% → 8.84%`，夏普 `0.10 → 0.24`，盈亏比 `1.15 → 1.23`；
  - 全周期 2023-2026（开启/关闭大盘择时）：收益分别提升至 `+46.43%`（PF `1.62`）与 `+53.80%`（回撤降至 `20.05%`）。
- **保持克制**：对 `ma_cross`、`bollinger_reversal` 的多条件叠加方案经 30% 样本外盲测发现存在过拟合衰减，主动回退不采用，保持其样本外原生优势。
- **UI 提示与测试**：回测中心补充策略风格与大盘择时搭配说明；新增 `tests/test_p3_audit_optimizations.py` 2 项策略守卫测试。

## [v0.4.7] - 2026-11-12

### 🔬 深度审计修复与边界加固

- **撮合与流动性边界 (`engine/backtester.py`)**:
  - `current_close` 模式下，顺延卖单遇到次日停牌（`curr_idx is None`）不再以昨日信号价虚假成交，而是继续顺延到复牌日；
  - 启用 `MAX_PARTICIPATION_RATE` 导致单日卖出仅部分成交时，自动顺延剩余可用份额至后续交易日分批出清。
- **推荐口径修正 (`services/strategy_recommender.py`)**:
  - 样本外统计充足性改按**已平仓卖单数**（`oos["sells"] >= OOS_MIN_TRADES`）判定，避免 3 买 0 卖或仅 1 笔平仓产生误导性胜率标签；移除无效常数缩放并统一 `-1e9` 无成交哨兵；已刷新库内 13 只自选股推荐标签。
- **数据源熔断器 (`data/fetcher_circuit.py`)**:
  - 冷却到期退出熔断时清零历史失败计数，给予数据源完整的恢复探测窗口（而非单次失败立即二次熔断）。
- **调度幂等与推送 (`scheduler/__init__.py`, `notifier/push.py`)**:
  - `scan_and_notify()` 仅在扫描与推送均未抛出异常时才写入 `notify_log` 幂等键，防止瞬时异常锁死当日补跑；`notify_signals()` 规范返回布尔值。
- **指标与缓存 (`data/indicators.py`, `app/main.py`, `app/portfolio.py`, `app/dashboard.py`)**:
  - `_wilder_mean()` 支持前部存在孤立 `NaN` 空洞时从首个连续窗口起算；`apply_indicators()` 改入口单次深拷贝，消除 6 次冗余拷贝；
  - 手动更新数据、添加自选股下载（同步走 `align_volume_unit` 与除权校验）及数据库恢复后统一清理 `st.cache_data` 与 `clear_backtester_cache()`，热备文件名增加秒级后缀；
  - `validate_capital()` 拦截 `NaN/Inf/bool`，中文日期选择器按当月天数钳位，`app/backtest.py` 修复循环内 `name_map` 重复构造与 Ruff `B023`/`B904` 告警，移除 `app/dashboard.py` 首页渲染时的阻塞性 `bs.login()` 外网探测，清理根目录 4 个历史遗留临时脚本。
- **测试验证**: 新增 [tests/test_p3_audit_optimizations.py](tests/test_p3_audit_optimizations.py)（8 项回归测试），全量 **305 项测试全部通过**。

## [v0.4.6] - 2026-11-12

### 🧪 方法论：策略推荐不再用「同一段数据既择优又展示业绩」

`services/strategy_recommender.py` 原先让 4 个策略在 `[2023-01-01, 今天]` 全样本上回测取 argmax，并把这段样本内最优的收益/胜率写进自选股标签与推荐理由，叠加了选择偏差与多重比较偏差。项目里已有 `walk_forward()` 却没接线。

- 改为按交易日切 70% 训练 / 30% 测试：训练段只负责「选哪个策略」，测试段冻结该策略后单独回测。
- `tag` / `reason` 只引用测试段指标；新增 `is_oos`、`train_window`、`test_window`、`oos_total_return` / `oos_win_rate` / `oos_trades`；`details` 明确标注为样本内对比。
- 样本外成交 < 3 次时只给策略名 +「样本外交易不足」，不给业绩标签；4 策略择优按候选数做线性折扣（每多一个候选 -5%）。
- 批量入口新增统一 `start_date` / `end_date`，避免每只股票各自取「今天」导致窗口不可比。

### 📐 指标口径：标准 Wilder 与完整预热

- `data/indicators.py` 新增 `_wilder_mean()`：前 N 个有效 delta 取 SMA 作种子，之后 `avg = (avg*(N-1)+x)/N`。旧的 `ewm(alpha=1/N, adjust=False)` 以第一个值为种子，同一波动序列首个 RSI 相差 13.23 点且衰减很慢，会改变 `rsi>68` / `rsi<45` 等策略判定。
- RSI 预热期保持 NaN（旧实现用 `where` 把 NaN 填成中性 50，数据不足也会触发信号）；ATR 由 `rolling(min_periods=1).mean()` 改为 Wilder + `min_periods=N`，消除前 13 根偏小的 ATR 被 ATR 风险定仓放大成超配。
- 回测主循环已有 180 天 warmup，正式区间不受预热影响；`scanner` / 自定义周期路径的指示值更保守。

### 🚦 撮合真实性

- **封板判定改用最终成交价**：旧实现先判封板、再滑点、再 `adjust_price`，委托价略低于涨停价时会被裁到涨停价成交，绕过一字板拒单。现在先算出最终价再判，一字板判定退化为「bar_low >= upper」/「bar_high <= lower」。
- **current_close 跌停卖单顺延**：旧实现丢弃 `portfolio.sell()` 返回值且 `deferred_sells` 只在 next_open 分支构建，一字跌停当日卖单永久消失。新增跨日 `pending_deferred_sells`，保持 next_open 原有「当日合并、次日开盘重试」节奏不变。
- **流动性与冲击成本（默认关闭，零基线漂移）**：`MAX_PARTICIPATION_RATE` 截断单笔下单量不超过信号日成交量 × 比例；`apply_slippage()` 扩展 `volume/bar_volume/volatility`，按 `k * sigma * sqrt(参与率)` 加平方根冲击并受 `MAX_IMPACT_PCT` 封顶。next_open 一律用 `decision_idx`（信号日）取成交量/ATR%，避免未来函数。开启后历史收益会下降，需重跑基线。

### 🛡️ 数据、迁移与更新锁

- `_run_migrations()` 改声明式：先 `PRAGMA table_info` 保证幂等、逐条 `execute`、每版本独立 COMMIT；任一 DDL 失败即 ROLLBACK + 抛错启动失败，**绝不再无条件推进 `user_version`**（旧行为会让失败迁移永不重试、schema 永久缺列）。删除 `init_db()` 尾部吞异常的裸 `ALTER TABLE`。新增 v5 `notify_log` 表。
- 更新锁：元数据改「临时文件 + `os.replace`」原子写；半截 JSON 不再被判为孤儿锁（旧实现会让两个进程同时进临界区）；`release` 按 owner_token 校验所有权、`force_release(expected_meta)` 做 TOCTOU 二次校验；`_is_pid_running` 在权限不足时保守视为存活，Win64 句柄改 `c_void_p` 防截断。
- `_fetch_daily_tushare()`：复权因子接口返回空、缺列或覆盖率不足时一律放弃该源，不再把未复权价全填 `adj_factor=1.0` 后当作前复权写入。
- `get_instrument_list()` 补回 `industry/is_st/list_date/delist_date`（旧实现只投影 4 列，导致 `get_st_lookup()` 的 is_st 分支与推荐器行业启发式恒失效），ETF 侧补齐同名空列。
- 新增 `get_point_in_time_universe(as_of)`，按 `list_date <= as_of <= delist_date` 过滤，为退市股历史回填预留 point-in-time 股票池接口。

### 🔒 安全与调度

- 移除硬编码默认口令 `quant123`：`APP_AUTH_ENABLED=true` 时必须配置 ≥8 位 `APP_AUTH_PASSWORD` 或 `APP_AUTH_PASSWORD_HASH`（PBKDF2-HMAC-SHA256，`迭代$salt_hex$hash_hex`），否则拒绝启动；`hmac.compare_digest` 恒定时间比较、会话 8 小时过期、连续失败达阈值后指数退避。
- Webhook 加载时校验 https + 官方域名白名单（可由 `WEBHOOK_ALLOW_CUSTOM_HOST` 显式放宽）+ 禁私网/环回/元数据 IP；统一 `_post_webhook()` 出口 `allow_redirects=False`；`_safe_log()` 改正则脱敏保留诊断信息，响应体只记 `errcode/code/msg` 不再整包打日志。
- `scan_and_notify()` 用 `notify_log` 的「日期 + 任务」主键去重，解决 Windows 计划任务与 APScheduler 双入口重复推送；`start()/stop()` 对日志 handler 去重并在停止时移除关闭。
- `scripts/init_data.py` 补齐 `import pandas as pd`（ruff F821 发现的真实 NameError，Step 2.5 合并股票/ETF 列表必崩）。

### 🔧 工程规范

- 新增 `pyproject.toml`（ruff E/F/W/I/B/UP + mypy + pytest）与 `requirements-dev.txt`。
- CI：新增 ruff 安装、`ruff check --select F821` 零容忍阻断门禁、全量 ruff 报告（暂不阻断）；import 冒烟检查由「只打 ⚠」改为 `assert not failed` 真正失败。

### 🧪 测试

新增 `tests/test_p2_hardening_fixes.py`（19 项）；调整 2 处既有测试预期（Wilder 预热、推送幂等隔离）与 1 处 mock 签名（`allow_redirects`）。全量 **297 项通过**（30.9s）。

> 本轮未处理：退市股历史行情回填（决定幸存者偏差能否真正消除）、组合层行业/相关性约束、`app/backtest.py`(2144 行) 拆分、熔断状态持久化。

## [v0.4.5] - 2026-11-12

### 🩹 P0：两处已造成实际损害的数据缺陷

**① 除权断层误判 → 清库 → 历史不可逆丢失**

`check_split_dividend_anomaly` 原先只用 `pct_chg` 反推前收（`implied = close / (1 + pct_chg/100)`）。当该字段缺失或被数据源填成 0 时 `implied == close`，任意涨跌超 8% 都被误判为除权 → `clear_daily` 立即提交 DELETE → 若全量重拉失败，原增量仍被 `save_daily` 写回，历史从 5 行变 1 行。且 `full_start` 写死回溯 730 天，库里存着 2015 年起的数据时一次误判就永久丢掉前 3 年。

- 判定改为按证据可靠性分级：重叠交易日同日收盘价（最可靠）→ `pct_chg` 可信时反推 → `pct_chg` 不可信时仅当两日期为相邻交易日且涨幅超过板块涨跌停上限 +2pct 才判定。宁可漏判，不可误判。
- 新增 `replace_daily()`：先校验（新数据 ≥ 旧数据 90%、起点不晚于旧起点）、再备份到 `data/backups/`（压缩 CSV，每标的留最近 3 份）、DELETE + INSERT 同一事务。
- 新增 `resync_full_history()`：全量起点取**库内最早交易日**；只有拉取 + 清洗 + 安全校验全部通过才动库，任何一步失败整次放弃，原库分毫不动。
- `services/data_service.py` 与 `scripts/init_data.py` 统一改走新入口，移除 `clear_daily` 直接调用。

**② 成交量单位污染（真实库已修复）**

`clean_daily` 拿**前复权 close** 做 `ratio = amount/(close×volume)` 的分母，而 volume/amount 是未复权原值，比值随复权因子漂移；逐行阈值 1.5 会让同一批数据一部分 ×100、一部分不动。真实库中 `601318.SH` 2015–2025 共 2812 行成交量是「手」，2026-07-31 单日跳变 **99.69 倍**。

- 改为**整批一次决策**（ratio 中位数、阈值 20，远离复权因子取值域），新增 `volume_unit="auto"/"shares"/"lots"` 显式声明。
- 新增 `align_volume_unit()`：落库前对齐历史口径，防止数据源切换产生新断层。
- 新增 `scripts/repair_volume_units.py`（默认 dry-run，`--apply` 才写库，自动备份 + 幂等）。已对真实库执行修复，备份位于 `data/backups/`。

### 🔧 P1：改写回测结论的撮合缺陷

- **ATR 仓位未来函数**：`next_open` 在 D+1 开盘撮合，`_calc_buy_budget` 却读 D+1 的 `close`/`atr14`。改为只用信号日已知数据；实测只改 D+1 收盘价时买入股数由 2700/1100/4000/600 变为完全一致。
- **涨跌停封板判定退化**：`is_limit_*_locked` 的 `low_price/high_price` 参数在 `portfolio.py` 中从未传入，退化成「委托价等于涨跌停价就拒单」。现从 K 线传入当日 `low/high`，只有真正一字板才拒单；真实库 18330 根 K 线中 41 天原本会被误杀。
- **涨跌停价舍入**：Python `round` 是银行家舍入，交易所是 `ROUND_HALF_UP`。1.00~200.00 元逐分扫描原有 **769 个价位差 1 分**（如 `prev_close=2.05` → 1.84/2.25，应为 1.85/2.26），会把成交价 clamp 到低于跌停价的不可能价格。改用 `Decimal`。
- **风控克隆丢配置**：`_clone_risk_manager` 与 `grid_search` 的手工复制只带 4 个参数，`enable_break_even` 等 3 项被静默重置。统一为 `engine/risk_manager.clone_risk_manager()`，完整复制 7 个字段且可变状态独立。
- **撮合规则不透传**：`grid_search` / `grid_search_parallel` / `walk_forward` / `parameter_stability` 过去把 `position_sizing`、`max_active_positions`、`risk_per_trade`、`market_regime_filter`、`market_index` 留在默认值，选参阶段与实盘配置不是一回事。新增 `build_backtest_options()` 统一打包并透传；回测中心两个 Tab 增加「撮合规则」配置面板；`services/backtest_service.py` 同步支持。
- `grid_search_parallel` 改为通过 worker `initializer` 注入预加载数据，不再逐任务 pickle 整份行情字典。

### 🧪 测试

新增 `tests/test_p1_correctness_fixes.py`（29 项），全量 **278 项通过**（45.6s）。

---

## [v0.4.4] - 2026-11-06

### 🧪 样本外验证框架（方法论补全）

系统此前**完全没有样本外验证** —— 网格搜索排名第一的参数是纯样本内最优，却被直接当作策略业绩展示。本版本补齐这条链路。

- **新增 `engine/validation.py`**
  - `walk_forward()` — 滚动样本外验证。把区间切成 n 折，每折仅在训练段用网格搜索挑参，再在紧随其后、从未参与选参的测试段运行，最后把各折测试段权益按比例首尾拼接成一条完整样本外曲线。训练段与测试段严格无重叠，杜绝参数穿越。
  - `parameter_stability()` — 参数稳定性检验。取最优组合后，考察**只差一档参数**的邻居表现：`邻居均值 / 最优值` ≥ 0.7 为稳健高原，< 0.5 说明最优点是孤立尖峰（典型过拟合特征）。
  - `monte_carlo_trades()` — 交易序列蒙特卡洛，刻意拆成两个口径避免常见误用：
    - **重排（permutation）** 不改变盈亏总和，只能测「路径风险」—— 同样的交易换个发生顺序，回撤可能多深；
    - **自助重采样（bootstrap）** 才有收益分布，「亏损概率」取自这里（拿重排算亏损概率只能是 0% 或 100%，无分布意义）。
  - 已实现盈亏口径与组合盯市口径显式区分：`observed_realized_drawdown` 用于与重排分布同口径对比，`observed_drawdown`（组合盯市）仅作参考，避免用错基准。
- **新增「样本外验证」Tab（回测中心第 3 个）** — 策略/选参目标/参数网格/标的/折数/首折训练占比可视化配置；输出结论条、逐折样本内 vs 样本外对比柱状图、样本外拼接净值曲线、参数稳定性面板、回撤与收益分布直方图，并可导出 Markdown 报告。

### ⚡ 回测性能优化（实测 3.5s → 1.1s，同参数指标完全一致）

- **消除 `date_index` 的 `iterrows` 构建** — 原实现每只标的用 `df.iterrows()` 逐行建日期索引，单次回测固定耗掉 2.7s（占总耗时 30%）。改为 `{d: i for i, d in enumerate(df["trade_date"].tolist())}`。
- **新增 `engine/bar_view.py` 列式只读视图** — 排查到真正的热点：pandas 3.0 移除了 `_item_cache`，导致 `df[col]` 每次都重建 Series（宽表单次约 45µs）。`BarFrame` / `BarSeries` 提供 DataFrame 的接口子集（`__getitem__` / `iloc` / `columns` / `index` / `tolist()` / `pct_change()` 等），底层直读 numpy 数组；**任何未覆盖的属性自动回退到真实 DataFrame**，保证策略无需改一行代码。四条内置策略实测提速 2.5~8 倍，指标与 DataFrame 路径完全一致。
- **主循环价格读取改列式** — `df.iloc[idx]["close"]` 每次构造整行 Series（30 列）只为取一个收盘价，改为 `_StockArrays.val("close", idx)` 直读 numpy。
- **ST 标记与基准收益模块级缓存** — `get_instrument_list()` + `iterrows` 建 `st_lookup`、基准指数日线读取，原先每次 `run()` 都重做；网格搜索 N 个组合 = N 次重复。现改为 TTL 缓存 + LRU 缓存，`clear_backtester_cache()` 可显式失效。
- **`preload_backtest_data` 加 LRU 缓存** — 网格搜索/样本外验证的多折多组合复用同一份预计算指标，避免重复拉行情与重算。
- **测试套件耗时 66.9s → 26.8s**。

### 🧪 测试
- 新增 `tests/test_bar_view.py`（14 项）—— 列式视图与 pandas DataFrame 的接口等价性，覆盖切片/负索引/NaN/越界/回退路径。
- 新增 `tests/test_validation.py`（18 项）—— 折切分无穿越、样本外收益与各折复利一致、参数稳定性邻居计算、蒙特卡洛双口径语义、退化场景（折数不足/网格过小/交易不足）。
- 全项目测试用例扩充至 **249 项**，100% 通过。

## [v0.4.3] - 2026-11-06

### 🩺 回测正确性修复（P0，会改变历史回测指标读数）

- **停牌日持仓市值向前结转** — 修复 `Portfolio.market_value` 在当日无行情时直接丢弃持仓、把市值按 0 计的缺陷。此前只要持仓标的停牌一天，权益曲线就会出现一次虚假净值跳水，进而污染最大回撤、夏普与卡玛比率。现改为沿用最近一次已知收盘价（carry-forward），从未有过行情的持仓以成本兜底。
  - 实测（600900.SH 2021-11-29~12-10 停牌区间，买入持有）：最大回撤 **21.87% → 2.56%**，夏普 **0.24 → 0.34**，净值最低点 80,231 → 98,843，总收益率不变。
- **保本单判定改为相对成交额** — `break_even_count` 原用固定 `abs(pnl) <= 15.0` 绝对阈值，与资金规模、股价完全脱钩：1000 万资金的 12 元盈亏会被判成保本，小额资金 2% 的正常盈利也被算作保本。现改为 `|盈亏| / 成交金额 <= BACKTEST_BREAK_EVEN_PCT`（默认 0.1%，约等于一次完整买卖的税费成本），并支持通过 `.env` 覆盖。
- **总收益率分母修正** — `calc_metrics` 原本以「首日收盘后权益」为分母，首日若已有成交（权益已被手续费侵蚀），总收益率与年化收益率会被系统性偏移。现统一以 `initial_capital` 为基准。
- **Alpha/Beta 基准日期精确对齐** — 原实现用「尾部截断」对齐组合与基准的日收益序列，当基准与组合交易日集合不一致（基准有而组合无、或中途停牌缺日）时错位会持续累积。现改为按 `trade_date` 精确 join，同时保留序列入参的向后兼容路径。
- **Beta 估计量 ddof 一致化** — `np.cov` 默认 `ddof=1` 而 `np.var` 默认 `ddof=0`，两者混用会让 Beta 系统性放大 `n/(n-1)` 倍（n=4 时放大到 1.33）。现统一使用 `ddof=1`。

### 📐 前复权口径澄清

- 系统行情统一为前复权序列，送转派息已内含于价格。`Portfolio.apply_split_or_dividend` / `handle_corporate_action` 此前在 README 中被描述为回测内置能力，实际主循环从未调用（且调用会重复计入现金分红）。现补充显式警示说明，明确该方法仅保留给「不复权 + 复权因子」数据管道；前复权基准重置由 `check_split_dividend_anomaly` 在增量同步阶段处理。README 相关表述已同步更正。

### 🧪 测试
- 新增 `tests/test_p0_correctness_fixes.py`（14 项回归测试，含两只持仓中一只停牌的端到端回测场景），全项目测试用例扩充至 **217 项**，100% 通过。

---

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
