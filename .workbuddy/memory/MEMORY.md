# A股量化交易系统 - 项目记忆

## 项目概况
- **路径**: `E:\wxf\claude\quant-trading\`
- **版本**: v0.4.4（2026-11-06 性能优化 + 样本外验证）
- **技术栈**: Streamlit + 自研 pandas 回测引擎 + 多数据源 + SQLite + Plotly
- **测试**: 249 项全通过（17 个模块，套件 24s）
- **Skill 位置**: 3 处已同步更新
  - `项目/.atomcode.md` — atomcode 项目指令（最详细）
  - `~/.claude/skills/quant-trading-system/SKILL.md` — atomcode 全局 skill
  - `E:\wxf\claude\skills\quant\SKILL.md` + `config.yaml` — 独立 skill 包

## 核心架构
- `core/` — config.py + models.py + exceptions.py（自定义异常层次）
- `data/` — fetcher.py(多源级联) / fetcher_base.py(DataSource ABC) / fetcher_circuit.py(CircuitBreaker+TokenBucket) / storage.py(SQLite+连接池+迁移) / indicators / cleaner
- `engine/` — backtester(零拷贝+网格搜索+列式快路径) / bar_view(列式只读视图) / validation(样本外验证) / portfolio / position / commission / scanner / risk_manager
- `services/` — DataService / BacktestService / SignalService / validators / strategy_recommender
- `strategies/` — 4 个精选冠军策略（kdj_cross / ma_cross / bollinger_reversal / signal_combo），importlib 自动发现
- `app/` — 7个页面 + 认证 + 响应式CSS + CSV导出
- `notifier/` — 5通道推送（Webhook日志脱敏）
- `tests/` — 249 个单元测试（全部通过）

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

## 注意事项
- 启动命令: `cd E:\wxf\claude\quant-trading && py run.py`
- SQLite 路径: `data/quant.db`，WAL模式 + 线程级连接池 + 文件锁
- 认证: 在 .env 设置 `APP_AUTH_ENABLED=true` + `APP_AUTH_PASSWORD=xxx`
- 数据缺口：`600900.SH` 在 2021-11-29~12-10 有 10 天停牌缺行，是验证市值结转的标准样本
