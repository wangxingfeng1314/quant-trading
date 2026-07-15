# 📊 A股量化交易系统

以 **自选股为中心** 的个人A股量化系统。支持多数据源、策略回测、信号扫描、一键跟单。  
加自选股 → 自动下载数据 → 自动扫信号，一条龙。

---

## 🚀 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 Token（可选，缺省用 AKShare 免费数据源）
cp .env.example .env
# 编辑 .env 填入你的 Tushare Token（如果不配置，自动降级到 AKShare / Baostock）

# 3. 首次初始化（先加自选股，再只下载自选股的数据）
py scripts/init_data.py --watchlist

# 4. 启动系统
py run.py
```

浏览器自动打开 http://localhost:8501

---

## 📖 完整使用指南

### 🏠 首页看板 — 每天先看这个

一屏掌握全局：大盘指数（上证/深证/创业板）、自选股快照（含趋势判断/RSI）、今日信号、自选股涨跌幅排行。

### 📈 数据浏览 — 看K线+指标

搜索股票 → 选日期范围 → 勾选指标（均线/MACD/布林带）→ 看K线图 + 顶部指标卡片。

### 🔬 回测中心 — 核心功能（4个Tab）

| Tab | 功能 | 说明 |
|-----|------|------|
| ① 运行回测 | 单次回测/批量自选股回测+基准对比 | 选策略→调参数→选股票→运行→看权益曲线/月度热力图/滚动夏普 |
| ② 参数优化 | 网格搜索+热力图 | 选策略→设优化目标→设参数范围→运行→看最优参数+2D热力图 |
| ③ 多策略对比 | 叠加权益曲线/矩阵热力图 | 对比单股多策略，或策略×自选股热力图 |
| ④ 历史记录 | 查看过往回测 | 展开查看详情+交易明细 |

**新增功能：**
- **批量回测自选股**：一键对所有自选股跑回测，对比表格 + 最佳标记
- **矩阵热力图**：策略×自选股，热力图展示收益分布
- **策略参数模板**：保存/加载参数配置，支持"标记当前参数"
- **绩效指标增强**：新增卡玛比率(Calmar)、Alpha、Beta

### 🔍 选股筛选 — 对自选股按因子排序

左侧设条件：均线多头/MACD金叉/RSI范围/放量/布林带/KDJ金叉/涨跌幅范围。
→ 点「开始筛选」→ 按综合评分排序 → 导出CSV

### 📡 信号中心 — 扫描+验证

| Tab | 功能 |
|-----|------|
| ① 实时扫描 | 选策略→选范围（自选股/全部有数据的股票）→ 开始扫描 |
| ② 历史信号 | 按策略/方向筛选 |
| ③ 信号验证 ⭐ | 验证信号后N日涨跌幅+胜率统计 |
| ④ 🎯 组合信号 | 多策略共识分析，买入/卖出共识+策略冲突检测 |

### 💼 持仓管理 — 自选股 + 跟单

| Tab | 功能 |
|-----|------|
| ① 自选股 | 添加/删除 + 自动下载数据 + 自动扫信号 + 分组管理 + K线快览 |
| ② 模拟持仓 | 手动记录持仓 + 自动算盈亏（**持久化到SQLite，刷新不丢**） |
| ③ 信号自动跟单 ⭐ | 一键跟入/跟出信号到持仓 + K线买卖点标注 |

---

## ⏰ 每日推荐工作流

```
打开系统 → 🏠 首页看自选股快照
         → 📡 信号中心扫自选股
         → 🔬 回测中心验证策略
         → 💼 持仓管理一键跟单
```

右上角侧边栏的 **🔄 更新自选股数据** 按钮，点击即可拉取最新数据（每次覆盖最近14天），带进度条和实时文字。

侧边栏还显示：
- 定时任务状态（下次运行时间）
- 数据库一键备份/恢复

---

## ⚙️ 数据管理

### 自选股模式（当前推荐）

系统以自选股为中心。**加多少只自选股，系统就管理多少只的数据。**  
加自选股时自动完成：

```
添加股票 → 写入自选股表 → 下载5年历史数据 → 扫描信号 → 页面刷新可见
```

### 常用命令

```bash
# 首次初始化（只下载自选股数据）
py scripts/init_data.py --watchlist

# 增量更新数据（只更新自选股，覆盖最近14天）
py scripts/init_data.py --update --days 30 --watchlist

# 或者用 UI 侧边栏的 "🔄 更新自选股数据" 按钮

# 全市场模式（不用自选股时，下载沪深300+前300只）
py scripts/init_data.py --stocks 300

# 断点续传（中途中断后继续）
py scripts/init_data.py --stocks 300 --resume
```

### 数据源级联

| 优先级 | 数据源 | 用途 | Token |
|:------:|--------|------|:-----:|
| 1 | **AKShare** 🏆 | 日线数据（前复权），主力免费，无限量 | ❌ |
| 2 | Tushare Pro | 股票列表（含行业）、复权因子 | ✅ 可选 |
| 3 | Baostock | 最后兜底 | ❌ |

### Windows 定时任务

系统已预设定时任务 `QuantTrading-DataUpdate`，每个工作日 **17:00** 自动更新数据：

```bash
# 查看任务状态
schtasks /query /tn QuantTrading-DataUpdate

# 手动触发
schtasks /run /tn QuantTrading-DataUpdate
```

---

## 🧩 系统架构

```
quant-trading/
├── app/                    # Streamlit 页面（7个功能页）
│   ├── main.py             # 主入口 + 导航 + 侧边栏（一键更新按钮）
│   ├── dashboard.py        # 🏠 首页看板
│   ├── data_viewer.py      # 📈 数据浏览/K线
│   ├── backtest.py         # 🔬 回测中心
│   ├── signal.py           # 📡 信号中心
│   ├── portfolio.py        # 💼 持仓管理
│   ├── screener.py         # 🔍 选股筛选
│   └── strategy_intro.py   # 📚 策略百科
├── core/                   # 配置 + 数据模型
│   ├── config.py           # .env 配置加载
│   └── models.py           # Signal, Trade, BacktestResult 等
├── data/                   # 数据层
│   ├── fetcher.py          # 多数据源级联：AKShare → Tushare → Baostock
│   ├── storage.py          # SQLite CRUD
│   ├── indicators.py       # 技术指标：MA/MACD/RSI/BOLL/KDJ/ATR
│   └── cleaner.py          # 数据清洗：OHLC校验、停牌过滤、去重
├── engine/                 # 回测引擎
│   ├── backtester.py       # 回测主循环 + grid_search() 网格搜索
│   ├── portfolio.py        # 组合管理（含滑点模型）
│   ├── position.py         # 持仓类（T+1规则）
│   ├── commission.py       # A股费用模型（佣金/印花税/过户费/滑点）
│   └── scanner.py          # 信号扫描器
├── strategies/             # 策略（插件式注册，共11个）
│   ├── base.py             # 策略抽象基类
│   ├── ma_cross.py         # 双均线交叉（趋势跟踪）
│   ├── macd_divergence.py  # MACD背离（反转）
│   ├── turtle.py           # 海龟突破（趋势跟踪）
│   ├── rsi_oversold.py     # RSI超卖反转（反转）
│   ├── bollinger_reversal.py # 布林带反转（反转）
│   ├── kdj_cross.py        # KDJ金叉/死叉（短线）
│   ├── ma_bullish.py       # 均线多头排列（趋势跟踪）
│   ├── donchian_breakout.py # 唐奇安通道突破（趋势跟踪）
│   ├── volume_price_breakout.py # 量价突破（动量）
│   ├── double_bottom.py    # 双底形态识别（反转）
│   └── multi_factor.py     # 多因子综合评分（组合）
├── scripts/                # 运维脚本
│   ├── init_data.py        # 数据初始化/增量更新
│   └── update_data.bat     # Windows定时任务脚本
├── scheduler/              # 定时调度
│   └── __init__.py         # APScheduler 每日定时更新
├── notifier/               # 消息推送
│   └── push.py             # Server酱/PushPlus
├── logs/                   # 日志（自动轮转，5MB × 3份）
├── .env                    # 配置文件（不提交）
└── data/quant.db           # SQLite 数据库（自动生成）
```

---

## 📦 依赖

```
streamlit>=1.28
pandas>=1.5
numpy>=1.24
plotly>=5.15
akshare>=1.10
tushare>=1.3
baostock>=0.8
apscheduler>=3.10
python-dotenv>=1.0
requests>=2.28
```

---

## ➕ 添加新策略（3步）

1. 在 `strategies/` 下新建文件，继承 `BaseStrategy`
2. 在 `strategies/__init__.py` 注册到 `STRATEGY_REGISTRY`
3. 重启 Streamlit，自动识别

```python
from strategies.base import BaseStrategy

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    description = "我的自定义策略"
    param_schema = {
        "period": {"default": 20, "desc": "计算周期"},
    }

    def on_bar(self, trade_date, data, portfolio=None):
        # 你的策略逻辑
        return signals
```

---

## ⚙️ 配置项（.env）

```
TUSHARE_TOKEN=xxx           # Tushare Pro token（可选，AKShare不需要）
SLIPPAGE_RATE=0.001         # 滑点比率
DATA_START_DATE=20210701     # 数据起始日期（首次下载从哪天开始）
DEFAULT_CAPITAL=100000      # 回测默认初始资金
COMMISSION_RATE=0.00025     # 佣金费率（万2.5）
STAMP_TAX_RATE=0.0005       # 印花税率（万5）
SCHEDULER_ENABLED=true      # 定时任务开关
SCHEDULER_HOUR=17           # 定时任务执行小时
```

---

## 💡 开发注意

- Python 3.11，Windows 下用 `py` 命令
- `.gitignore` 已排除 `__pycache__/`、`*.db`、`.env`、`logs/`
- 日志轮转：5MB 自动归档，保留 3 份备份
- 所有 DB 操作只通过 `data/storage.py`
