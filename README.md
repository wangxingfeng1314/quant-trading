<p align="center">
  <h1 align="center">📊 A股量化交易系统</h1>
  <p align="center">
    以<strong>自选股为中心</strong>的个人 A 股量化系统
    <br />
    多数据源 · 策略回测 · 信号扫描 · 一键跟单
    <br />
    <br />
    <a href="#-快速启动"><strong>🚀 快速启动 »</strong></a>
    ·
    <a href="#-功能页面"><strong>📖 功能指南 »</strong></a>
    ·
    <a href="#-策略体系"><strong>🧩 11个策略 »</strong></a>
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v0.3.0-blue" alt="version" />
  <img src="https://img.shields.io/badge/python-3.11-green" alt="python" />
  <img src="https://img.shields.io/badge/tests-89%20passed-brightgreen" alt="tests" />
  <img src="https://img.shields.io/badge/streamlit-1.58-red" alt="streamlit" />
</p>

---

## 📋 目录

- [🚀 快速启动](#-快速启动)
- [💡 设计理念](#-设计理念)
- [📖 功能页面](#-功能页面)
- [🧩 策略体系](#-策略体系)
- [⚙️ 数据管理](#️-数据管理)
- [🔬 回测引擎](#-回测引擎)
- [📡 消息推送](#-消息推送)
- [🧪 测试与CI/CD](#-测试与cicd)
- [📦 依赖](#-依赖)
- [🏗️ 系统架构](#️-系统架构)
- [➕ 添加新策略](#-添加新策略)
- [📜 Changelog](#-changelog)

---

## 🚀 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置（可选，缺省用 AKShare 免费数据源）
cp .env.example .env

# 3. 首次初始化（先加自选股，然后只下载自选股数据）
py scripts/init_data.py --watchlist

# 4. 启动系统
py run.py
```

浏览器自动打开 **http://localhost:8501**

### ⏰ 每日推荐工作流

```
17:00 定时任务自动更新数据
  ↓
🏠 首页看板 → 看大盘 + 数据健康度 + 自选股快照
  ↓
📡 信号中心 → 扫描今日信号
  ↓
🔍 选股筛选 → 找符合形态标的
  ↓
🔬 回测中心 → 验证策略参数（支持并行网格搜索）
  ↓
💼 持仓管理 → 一键跟单
```

---

## 💡 设计理念

**自选股中心化**——系统所有功能围绕你的自选股展开，不淹没在全市场 5000+ 只股票里。

```
加自选股 → 自动下载数据 → 自动扫描信号 → 页面刷新可见
```

| 对比项 | 传统全市场扫描 | ✅ 本系统（自选股中心） |
|:-------|:-------------|:---------------------|
| 数据量 | 5000+ 只 | 你关注的 N 只 |
| 扫描速度 | 几分钟 | 几秒 |
| 关注度 | 分散 | 聚焦 |
| 信号噪音 | 大量无效信号 | 只产生你关心的信号 |

---

## 📖 功能页面

### 🏠 首页看板
一屏掌握全局：大盘指数（上证/深证/创业板）、**数据健康监控面板**（含数据库完整性检查）、自选股快照（趋势/RSI/MA20）、今日信号排行、涨跌幅榜。

> **数据健康面板**：5 个指标卡片 + 数据源状态 + 自选股健康度明细 + 一键更新 + 导出健康报告 + 数据库完整性检查。

### 📈 数据浏览
搜索股票 → 选日期范围 → 勾选指标 → 看K线 + 顶部指标卡片。
- 支持：均线(MA5/20/60)、MACD、布林带、RSI
- 默认只列出有数据的股票

### 🔬 回测中心（4个Tab）

| Tab | 功能 | 说明 |
|:----|:-----|:------|
| ① **运行回测** | 单次回测/批量自选股 | 选策略→调参数→选股票→运行→权益曲线+月度热力图+滚动夏普 |
| ② **参数优化** | 网格搜索(串行+并行) | 选策略→设优化目标→设参数范围→运行→热力图+最佳参数 |
| ③ **多策略对比** | 叠加权益曲线 | 对比单股多策略效果 |
| ④ **历史记录** | 过往回测 | 展开查看详情+交易明细 |

**回测特性：**
- ✅ A股费用模型（佣金万2.5/最低5元/印花万5/过户费）
- ✅ 滑点模型（可配置千分之一）
- ✅ 涨跌停限制（主板±10%/ST±5%/创业板±20%）
- ✅ T+1 规则
- ✅ 评分仓位管理
- ✅ 沪深300基准对比
- ✅ 绩效归因（月度热力图/逐年收益/滚动夏普/回撤）
- ✅ 导出报告
- 🚀 **网格搜索并行化**：多核 CPU 加速参数搜索

### 🔍 选股筛选
按技术因子筛选：均线多头/MACD金叉/RSI范围/放量/布林带/KDJ金叉/涨跌幅。
→ 综合评分排序 → 导出 CSV

### 📡 信号中心（4个Tab）

| Tab | 功能 |
|:----|:------|
| ① **实时扫描** | 选策略+范围（自选股/全部有数据股票），带进度条 |
| ② **历史信号** | 按策略/方向筛选 |
| ③ **信号验证 ⭐** | 验证信号后 N 日涨跌幅 + 胜率统计 |
| ④ **组合信号** | 多策略共识分析 + 冲突检测 |

### 💼 持仓管理（3个Tab）

| Tab | 功能 |
|:----|:------|
| ① **自选股** | 添加/删除 + 自动下载数据 + 自动扫信号 + 分组管理(长线/短线) |
| ② **模拟持仓** | 手动记录持仓 + 自动算盈亏（**持久化到 SQLite，刷新不丢**） |
| ③ **信号自动跟单 ⭐** | 一键跟入/跟出信号到持仓 + K线买卖点标注 |

### 📚 策略百科
策略说明 + 参数详情 + 历史回测排行榜。

---

## 🧩 策略体系

### 趋势跟踪（4个）
| 策略 | 文件 | 核心逻辑 |
|:-----|:-----|:---------|
| **双均线交叉** | `ma_cross.py` | 快线上穿慢线买入，下穿卖出 |
| **均线多头排列** | `ma_bullish.py` | MA5 > MA20 > MA60 确认上升趋势入场 |
| **海龟突破** | `turtle.py` | 突破 N 日高点买入，跌破 N 日低点卖出 |
| **唐奇安通道突破** | `donchian_breakout.py` | 通道突破 + ATR 动态止损 |

### 反转交易（4个）
| 策略 | 文件 | 核心逻辑 |
|:-----|:-----|:---------|
| **MACD背离** | `macd_divergence.py` | 价格新低 + MACD 底背离买入 |
| **RSI超买超卖** | `rsi_oversold.py` | RSI < 30 买入，> 70 卖出 |
| **布林带反转** | `bollinger_reversal.py` | 触下轨买入，触上轨卖出 |
| **双底形态识别** | `double_bottom.py` | 自动检测 W 底 + 突破颈线买入 |

### 动量（2个）
| 策略 | 文件 | 核心逻辑 |
|:-----|:-----|:---------|
| **KDJ金叉死叉** | `kdj_cross.py` | 低位金叉买入，高位死叉卖出 |
| **量价突破** | `volume_price_breakout.py` | 放量突破均线买入，缩量反弹卖出 |

### 组合（1个）
| 策略 | 文件 | 核心逻辑 |
|:-----|:-----|:---------|
| **多因子综合评分** | `multi_factor.py` | 5 因子加权打分（均线+MACD+RSI+量能+布林） |

---

## ⚙️ 数据管理

### 数据源级联

```
AKShare（主力，免费前复权）
  └─ 失败 → Tushare Pro（备用，需Token）
       └─ 失败 → Baostock（兜底，免费无限量）
```

| 数据源 | 用途 | Token | 熔断保护 | 重试策略 |
|:------:|:-----|:-----:|:---------|:---------|
| **AKShare** 🏆 | 日线/指数/成分股 | ❌ | 连续20次失败→熔断300s | 3次，指数退避+随机 |
| **Tushare Pro** | 股票列表(含行业)/复权因子 | ✅ | — | 2次重试 |
| **Baostock** | 日线最后兜底 | ❌ | — | 级联自然兜底 |

### 常用命令

```bash
# 自选股初始化
py scripts/init_data.py --watchlist

# 增量更新
py scripts/init_data.py --update --days 30 --watchlist

# 全市场模式
py scripts/init_data.py --stocks 300 --resume   # 断点续传
```

### Windows 定时任务

| 任务名 | 触发时间 | 脚本 |
|:-------|:---------|:-----|
| `QuantTrading-DataUpdate` | 每个工作日 17:00 | `scripts/update_data.bat` |

```bash
schtasks /query /tn QuantTrading-DataUpdate  # 查看状态
schtasks /run /tn QuantTrading-DataUpdate   # 手动触发
```

也可以在 Streamlit UI 侧边栏点击 **「🔄 更新自选股数据」** 按钮（带进度条）。

### 配置项（.env）

```
# 数据源
TUSHARE_TOKEN=               # 可选，AKShare不需要

# 回测参数
SLIPPAGE_RATE=0.001          # 滑点千分之一
COMMISSION_RATE=0.00025      # 佣金万2.5
STAMP_TAX_RATE=0.0005        # 印花税万5
DEFAULT_CAPITAL=100000       # 默认资金10万
DATA_START_DATE=20210701     # 数据起始日期

# 定时调度
SCHEDULER_ENABLED=true
SCHEDULER_HOUR=17

# 推送通道（可选）
WECOM_WEBHOOK=               # 企业微信机器人 Webhook
DINGTALK_WEBHOOK=            # 钉钉机器人 Webhook
DINGTALK_SECRET=             # 钉钉加签密钥
SERVER_CHAN_KEY=             # Server酱 Key
PUSHPLUS_TOKEN=              # PushPlus Token
```

---

## 🔬 回测引擎

### 核心特性
- **逐日迭代**：加载数据 → 构建日期序列 → 逐日产生信号 → 执行交易 → 记录权益
- **A股费用模型**：佣金万2.5（最低5元）、印花税万5（仅卖出）、过户费万0.1、滑点千1
- **风控规则**：涨跌停限制、T+1、评分仓位管理（5%~20%）
- **绩效指标**：总收益、年化收益、最大回撤、夏普比率、卡玛比率(Calmar)、Alpha、Beta、胜率
- **网格搜索**：`grid_search()`（串行）+ `grid_search_parallel()`（并行，利用多核 CPU）
- **基准对比**：沪深300归一化权益曲线

### 网格搜索并行化

```python
from engine.backtester import grid_search_parallel

results = grid_search_parallel(
    strategy_cls=MyStrategy,
    universe=["000001.SZ"],
    start_date="20240101", end_date="20241231",
    param_grid={"fast": [5, 10, 20], "slow": [30, 60, 120]},
    metric="sharpe_ratio",
    max_workers=4,   # 默认 = CPU 核心数
)
```

---

## 📡 消息推送

支持 **4 通道** 自动选择（至少一个成功即返回 True）：

| 通道 | 环境变量 | 配置位置 |
|:-----|:---------|:---------|
| 🔔 Server酱 | `SERVER_CHAN_KEY` | .env |
| 📱 PushPlus | `PUSHPLUS_TOKEN` | .env |
| 💬 企业微信机器人 | `WECOM_WEBHOOK` | .env |
| 🤖 钉钉机器人 | `DINGTALK_WEBHOOK` + `DINGTALK_SECRET` | .env |

推送内容：信号日报（TOP5买入/卖出）、回测结果通知、**每日持仓盈亏日报**。

### 数据源 & API 限速
- **Tushare**：令牌桶算法限速（`_TokenBucket`），支持突发请求，长期平均速率稳定
- **AKShare**：指数退避重试（3次）+ 熔断保护（连续20次失败→跳过300s）

---

## 🧪 测试与CI/CD

### 单元测试（89个）

```bash
# 运行全部测试
py -m pytest tests/ -v

# 运行单个文件
py -m pytest tests/test_commission.py -v
```

| 测试文件 | 测试数 | 覆盖内容 |
|:---------|:------|:---------|
| `test_models.py` | 9 | Signal/Trade/BacktestResult/StockInfo 数据模型 |
| `test_commission.py` | 15 | 费用计算/滑点/涨跌停/取整手（15个边界测试） |
| `test_portfolio.py` | 14 | 买入/卖出/权益计算/绩效指标/边界场景 |
| `test_backtester.py` | 11 | 回测主循环/绩效验证/网格搜索（串行+并行） |
| `test_backtester_integration.py` | 5 | 真实 cleaner+indicators 集成测试 |
| `test_storage.py` | 24 | SQLite 7张表 CRUD 全覆盖 |
| `test_schema_alignment.py` | 11 | 模型-数据库表结构对齐验证 |

### CI/CD

项目已配置 GitHub Actions（`.github/workflows/ci.yml`），每次 push/PR 自动：

```
✅ 语法检查（py_compile 全量扫描 50+ 文件）
✅ pytest 全部 49 个测试
✅ 关键模块 import 一致性验证
```

---

## 📦 依赖

所有依赖已锁定版本，见 `requirements.txt`。

### 核心
| 包 | 版本 | 用途 |
|:---|:----|:------|
| streamlit | 1.58.0 | Web UI 框架 |
| pandas | 3.0.3 | 数据处理 |
| numpy | 2.2.4 | 数值计算 |
| plotly | 6.8.0 | 交互图表 |

### 数据源
| 包 | 版本 | 用途 |
|:---|:----|:------|
| akshare | 1.18.64 | 🏆 主力数据源 |
| tushare | 1.4.29 | 备用数据源 |
| baostock | 0.9.2 | 兜底数据源 |
| requests | 2.33.1 | HTTP 请求 |

### 其他
| 包 | 版本 | 用途 |
|:---|:----|:------|
| apscheduler | 3.11.2 | 定时调度 |
| python-dotenv | 1.2.2 | 配置管理 |
| pytest | 9.1.1 | 单元测试 |

---

## 🏗️ 系统架构

```
quant-trading/
├── app/                      # Streamlit 7 个功能页面
│   ├── main.py               # 主入口 + 侧边栏
│   ├── dashboard.py          # 🏠 首页看板（含数据健康面板）
│   ├── data_viewer.py        # 📈 数据浏览/K线
│   ├── backtest.py           # 🔬 回测中心（+ 并行网格搜索 UI）
│   ├── signal.py             # 📡 信号中心
│   ├── portfolio.py          # 💼 持仓管理
│   ├── screener.py           # 🔍 选股筛选
│   └── strategy_intro.py     # 📚 策略百科
├── core/                     # 配置 + 数据模型
│   ├── config.py             # .env 配置加载
│   └── models.py             # Signal/Trade/BacktestResult/StockInfo
├── data/                     # 数据层
│   ├── fetcher.py            # 多源级联 + 熔断保护 + 自动重试
│   ├── storage.py            # SQLite CRUD
│   ├── indicators.py         # 技术指标（MA/MACD/RSI/BOLL/KDJ/ATR）
│   └── cleaner.py            # 数据清洗（OHLC校验/停牌过滤/去重）
├── engine/                   # 回测引擎
│   ├── backtester.py         # 回测主循环 + grid_search(串行+并行)
│   ├── portfolio.py          # 组合管理（滑点/费用）
│   ├── position.py           # 持仓类（T+1规则）
│   ├── commission.py         # A股费用模型（佣金/印花税/过户费）
│   └── scanner.py            # 信号扫描器（异常可见的 warn 级别日志）
├── strategies/               # 11 个策略（插件式注册）
├── scripts/                  # 运维脚本
├── scheduler/                # APScheduler 定时调度
├── notifier/                 # 消息推送（4通道：Server酱/PushPlus/企微/钉钉）
├── tests/                    # 49 个单元测试
├── .github/workflows/        # CI/CD
├── CHANGELOG.md              # 更新日志
└── requirements.txt          # 版本锁定
```

---

## ➕ 添加新策略

3 步完成：

```python
# 1. strategies/my_strategy.py 新建文件
from strategies.base import BaseStrategy
from core.models import Signal

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    description = "自定义策略说明"
    param_schema = {
        "period": {"default": 20, "desc": "计算周期"},
    }

    def on_bar(self, trade_date, data, portfolio=None):
        signals = []
        for ts_code, df in data.items():
            row = df.iloc[-1]
            # 你的信号逻辑
            signals.append(Signal(
                ts_code=ts_code, trade_date=row["trade_date"],
                strategy=self.name, direction="BUY",
                score=0.8, price_ref=row["close"],
                reason="信号原因",
            ))
        return signals

# 2. strategies/__init__.py 注册一行
#    在 STRATEGY_REGISTRY 中加入:
#    "my_strategy": MyStrategy,

# 3. 重启 Streamlit，自动识别
```

---

## 📜 Changelog

详见 [CHANGELOG.md](./CHANGELOG.md)

### v0.3.0 关键更新
- 🔄 **自选股中心化重构** — 所有功能围绕自选股
- 🧩 **11个策略** — 趋势跟踪(4)/反转(4)/动量(2)/组合(1)
- 🚀 **并行网格搜索** — 多核 CPU 加速参数优化
- 🧪 **89个单元测试+集成测试** — 7个测试文件全覆盖
- 🩺 **数据健康面板 + 数据库完整性检查** — 启动时自动校验
- 💬 **4通道推送 + 持仓日报** — 信号推送 + 每日盈亏推送
- 🔧 **数据获取增强** — Tushare/指数自动重试 + 令牌桶限速
- 🔐 **SQLite 锁优化** — WAL模式 + timeout + 文件锁防双写
- ⚡ **信号扫描缓存** — 当天已扫过的直接返回数据库结果
- 🤖 **策略自动发现** — importlib 扫描目录，新增策略零配置
- 📋 **CI/CD + 模型对齐测试** — GitHub Actions + 表结构验证

---

<p align="center">
  Made with ❤️ for A股量化 | v0.3.0
</p>
