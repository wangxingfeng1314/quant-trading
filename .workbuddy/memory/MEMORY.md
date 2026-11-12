# A股量化交易系统 - 项目记忆

## 项目概况
- **路径**: `E:\wxf\claude\quant-trading\`
- **版本**: v0.4.6（2026-11-12 第四批：策略样本外、指标口径、锁/迁移/推送安全、工程规范）
- **技术栈**: Streamlit + 自研 pandas 回测引擎 + 多数据源 + SQLite + Plotly
- **测试**: 297 项全通过（19 个模块，套件 ~31s）
- **Skill 位置**: 3 处已同步更新
  - `项目/.atomcode.md` — atomcode 项目指令（最详细）
  - `~/.claude/skills/quant-trading-system/SKILL.md` — atomcode 全局 skill
  - `E:\wxf\claude\skills\quant\SKILL.md` + `config.yaml` — 独立 skill 包

## 核心架构
- `core/` — config.py + models.py + exceptions.py（自定义异常层次）
- `data/` — fetcher.py(多源级联) / fetcher_base.py(DataSource ABC) / fetcher_circuit.py(CircuitBreaker+TokenBucket) / storage.py(SQLite+连接池+迁移+安全全量替换) / indicators / cleaner(整批单位归一化)
- `engine/` — backtester(零拷贝+网格搜索+列式快路径+撮合规则透传) / bar_view(列式只读视图) / validation(样本外验证) / portfolio / position / commission(交易所舍入+封板判定) / scanner / risk_manager(clone_risk_manager)
- `services/` — DataService / BacktestService / SignalService / validators / strategy_recommender
- `strategies/` — 4 个精选冠军策略（kdj_cross / ma_cross / bollinger_reversal / signal_combo），importlib 自动发现
- `app/` — 7个页面 + 认证 + 响应式CSS + CSV导出
- `notifier/` — 5通道推送（Webhook日志脱敏）
- `tests/` — 278 个单元测试（全部通过）

## 数据源级联（优先级）
1. AKShare (主力, 免费, 前复权, 熔断保护: 20次失败→300s)
2. TickFlow (备用, 需API Key, 前复权)
3. Tushare Pro (备用, 需Token, 含行业)
4. Baostock (兜底, 免费)

## A股费用模型
- 佣金万2.5(最低5元) / 印花税万5(仅卖出) / 过户费万0.1 / 滑点千1
- 涨跌停: 主板±10% / ST±5% / 创业板科创板±20% / T+1

## 回测口径约定（重要，勿破坏）
- **行情统一前复权**：送转派息已内含于价格，回测**不得**再调用 `handle_corporate_action` / `apply_split_or_dividend`（会双计分红）
- **停牌日市值 carry-forward**：`Portfolio.last_prices` 保存最近已知价，缺行情不得按 0 计
- **收益基准**：`calc_metrics` 用 `initial_capital` 作分母，非首日收盘权益
- **基准对齐**：`benchmark_returns` 传 `{trade_date: ret}` dict，按日期 join；Beta 用 `ddof=1`
- **涨跌停价必须 `ROUND_HALF_UP`**：Python `round` 是银行家舍入，交易所不是；1~200 元区间曾有 769 个价位差 1 分
- **封板判定必须传当日 `bar_low`/`bar_high`**：只有一字板才拒单，否则「触及涨跌停价」会被误当成封死
- **ATR 仓位只用信号日数据**：`next_open` 在 D+1 开盘撮合，绝不能读 D+1 的 close/atr14
- **撮合规则必须透传**：网格搜索 / walk_forward / 参数稳定性一律经 `build_backtest_options()` 拿配置，否则选参阶段与实盘不是一回事
- **风控克隆统一用 `clone_risk_manager()`**：手工复制会漏掉 `enable_break_even` 等 3 个字段，导致训练段与验证段配置不同

## 数据口径约定（勿退回）
- **禁止 `clear_daily` + 重拉**：全量替换必须走 `replace_daily()`（校验+备份+单事务）与 `resync_full_history()`（重拉失败整次放弃、绝不写回断裂数据）
- **断层判定不可只用 `pct_chg` 反推**：字段为 0/缺失时会把合法涨停误判成除权进而清库。现按重叠日同日价格 → 可信涨跌幅 → 相邻交易日超涨跌停三级证据判定，宁可漏判不可误判
- **全量重拉起点取库内最早日**，不是固定回溯 730 天
- **成交量单位整批判定**：`clean_daily` 用 ratio 中位数一次决策（阈值 20），**不可用前复权 close 做逐行分母**（复权因子会漂移）。落库前经 `align_volume_unit()` 对齐历史口径
- **真实库已修复**：`601318.SH` 2015–2025 共 2812 行「手」→「股」，消除 2026-07-31 的 99.69 倍断层；备份 `data/backups/quant_before_volume_repair_20261112_114001.db`。修复脚本 `scripts/repair_volume_units.py`（默认 dry-run）

## 性能约定（勿退回）
- **不要用 `iterrows` 建索引**：`{d: i for i, d in enumerate(df["trade_date"].tolist())}`
- **不要在主循环里 `df.iloc[i]["col"]`**：用 `_StockArrays.val(col, i)` 直读 numpy
- **pandas 3.0 移除 `_item_cache`**：`df[col]` 宽表每次重建 Series ≈45µs，是历史热点根因
- `engine/bar_view.py` 的 `BarFrame`/`BarSeries` 只实现 DataFrame 子集，未覆盖属性走 `__getattr__` 回退真实 DataFrame（保证策略零改动）
- `clear_backtester_cache()` 用于测试隔离 ST/基准/预加载缓存

## 方法论约束（勿删）
- **样本外验证是必需环节**：网格搜索第一名是纯样本内最优，不得当作业绩展示
- **蒙特卡洛必须双口径**：重排只测路径风险（求和不变），亏损概率必须取自助重采样
- **参数稳定性**：邻居均值/最优值 <0.5 视为孤立尖峰

## v0.4.6 口径与约定（勿退回）
- **策略推荐必须样本外**：训练段 70% 选策略、测试段 30% 冻结并单独回测；`tag`/`reason` 只用 `oos_*`，`details` 是样本内。不得退回全样本 argmax 展示业绩
- **RSI/ATR 是标准 Wilder**：前 N 个 delta 取 SMA 作种子后递推，预热期 NaN；`ewm(alpha=1/N, adjust=False)` 不是 Wilder，首值可差 13 点
- **封板判定用最终成交价**：先滑点 + `adjust_price` 再判一字板；`current_close` 跌停卖单经 `pending_deferred_sells` 跨日重试（next_open 仍当日合并）
- **流动性与冲击成本默认关闭**：`MAX_PARTICIPATION_RATE=0`、`SLIPPAGE_IMPACT_K=0` 保持基线；开启后历史收益下降需重跑。next_open 取 `decision_idx` 的 volume/atr_pct，禁止用执行日数据
- **迁移失败绝不推进 user_version**：`_MIGRATIONS` 声明式、逐版本 COMMIT、失败 ROLLBACK + 抛错；v5 新增 `notify_log` 表
- **更新锁所有权**：lock_info.json 原子写、半截 JSON 不判陈旧、release 校验 owner_token、Windows 句柄用 `c_void_p`
- **`get_instrument_list()` 必须带 industry/is_st/list_date/delist_date**：丢列会让 ST 判定与行业启发式静默失效
- **认证无默认口令**：`APP_AUTH_ENABLED=true` 必须配 ≥8 位密码或 PBKDF2 哈希；Webhook 只收 https + 官方域名 + 禁私网/重定向
- **CI 门禁**：`ruff check --select F821` 零容忍（已修 `scripts/init_data.py` 的 `pd` NameError）；全量 ruff 报告暂不阻断

## 未做（需排期）
- 幸存者偏差：需回填退市股历史行情；`get_point_in_time_universe()` 已就绪但库内 `delist_date` 全空
- 组合层行业/相关性约束、熔断器状态持久化、`app/backtest.py`(2144 行) 拆分

## 注意事项
- 启动命令: `cd E:\wxf\claude\quant-trading && py run.py`
- SQLite 路径: `data/quant.db`，WAL模式 + 线程级连接池 + 文件锁
- 认证: 在 .env 设置 `APP_AUTH_ENABLED=true` + `APP_AUTH_PASSWORD=xxx`
- 数据缺口：`600900.SH` 在 2021-11-29~12-10 有 10 天停牌缺行，是验证市值结转的标准样本
- v0.4.5 修复会改变历史回测指标，`backtest_result` 表中的旧记录建议重跑
- 沙箱内 `app/main.py` 启动调 `schtasks.exe` 被黑名单拦截（容错已捕获，勿绕过），是测试套件耗时波动的根因
- Windows 上 heredoc stdin 脚本不能用 `ProcessPoolExecutor`（spawn 报 `<stdin>`），改用 `py -c` 或临时 .py 文件
