# A股量化交易系统 - 项目记忆

## 项目概况
- **路径**: `E:\wxf\claude\quant-trading\`
- **版本**: v0.3.1（2026-08-11 全面优化升级）
- **技术栈**: Streamlit + 自研 pandas 回测引擎 + 多数据源 + SQLite + Plotly
- **Skill 位置**: 3 处已同步更新
  - `项目/.atomcode.md` — atomcode 项目指令（最详细）
  - `~/.claude/skills/quant-trading-system/SKILL.md` — atomcode 全局 skill
  - `E:\wxf\claude\skills\quant\SKILL.md` + `config.yaml` — 独立 skill 包

## 核心架构（v0.3.1 更新）
- `core/` — config.py + models.py + exceptions.py（自定义异常层次）
- `data/` — fetcher.py(多源级联) / fetcher_base.py(DataSource ABC) / fetcher_circuit.py(CircuitBreaker+TokenBucket) / storage.py(SQLite+连接池+迁移) / indicators / cleaner
- `engine/` — backtester(零拷贝+网格搜索) / portfolio / position / commission / scanner(并行扫描+批量保存)
- `services/` — DataService / BacktestService / SignalService + validators.py（输入校验）【新增】
- `strategies/` — 17个策略, importlib 自动发现
- `app/` — 7个页面 + 认证 + 响应式CSS + CSV导出
- `notifier/` — 5通道推送（Webhook日志脱敏）
- `tests/` — 89个单元测试（全部通过）

## v0.3.1 优化要点
- 回测引擎: `df.iloc[:idx+1]` 替代 `.copy()`，零拷贝传视图
- 扫描器: ThreadPoolExecutor 并行 + `save_signals_batch()` 批量写入
- SQLite: `threading.local()` 连接池 + `PRAGMA user_version` 版本化迁移
- 安全: `APP_AUTH_ENABLED` 登录认证 + Webhook URL 脱敏
- 架构: DataSource ABC + Service 层 + 自定义异常 + 魔法数字配置化

## 数据源级联（优先级）
1. AKShare (主力, 免费, 前复权, 熔断保护: 20次失败→300s)
2. TickFlow (备用, 需API Key, 前复权)
3. Tushare Pro (备用, 需Token, 含行业)
4. Baostock (兜底, 免费)

## A股费用模型
- 佣金万2.5(最低5元) / 印花税万5(仅卖出) / 过户费万0.1 / 滑点千1
- 涨跌停: 主板±10% / ST±5% / 创业板科创板±20% / T+1

## 注意事项
- 启动命令: `cd E:\wxf\claude\quant-trading && py run.py`
- SQLite 路径: `data/quant.db`，WAL模式 + 线程级连接池 + 文件锁
- 认证: 在 .env 设置 `APP_AUTH_ENABLED=true` + `APP_AUTH_PASSWORD=xxx`
- 所有 skill 文件已于 2026-08-11 同步更新到最新代码状态
