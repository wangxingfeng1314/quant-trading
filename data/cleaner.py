"""数据清洗与验证

本模块负责对从各数据源获取到的原始数据进行清洗和标准化。
主要功能：
  1. 成交量/额单位归一化（兼容不同AKShare版本格式）
  2. OHLC 关系验证与修正
  3. 异常值剔除（价格≤0、停牌、空值等）
  4. 去重与排序

所有清洗函数保持纯函数风格：输入 DataFrame → 输出清洗后的 DataFrame。
"""
import logging          # 日志记录
import pandas as pd     # 数据处理
import numpy as np      # 数值计算

# 初始化日志记录器
logger = logging.getLogger(__name__)

# ============================================================
# 成交量/额归一化阈值常量
#   ratio = amount / (close * volume)
#
# ⚠️ close 是**前复权价**，而 amount/volume 是**未复权成交原价**，两者相差一个
# 随除权事件阶梯变化的复权因子 f（A股长期分红股常见 1~3）。因此 ratio 不是常数，
# 不能用 1.5 这种贴近 1 的阈值逐行判定 —— 那样会让同一批数据里一部分行 ×100、
# 另一部分不动，直接制造成交量单位混用（历史上 601318.SH 就出现 99.7 倍断层）。
#
# 判定改为：整批取 ratio 中位数做**一次**决策，阈值放在 100 量级：
#   ratio_med >= LOTS_RATIO_THRESHOLD(20) → volume 是「手」→ 整批 ×100
#   ratio_med <= OLD_FORMAT_RATIO_THRESHOLD(0.15) → 旧格式 → V×100, Amt×1000
#   其余 → 已是「股」，保持不动（f 在 1~20 内漂移都不会误判）
# ============================================================
OLD_FORMAT_RATIO_THRESHOLD = 0.15    # 旧格式: V手, Amt千元 → ratio≈0.1
LOTS_RATIO_THRESHOLD = 20.0          # V手+Amt元 → ratio≈100（含复权因子）


def clean_daily(df: pd.DataFrame, volume_unit: str = "auto") -> pd.DataFrame:
    """清洗日线数据，确保数据质量

    Args:
        volume_unit: 数据源显式声明的成交量单位，避免启发式误判：
            ``"shares"`` / ``"股"`` → 已是股，不做手→股换算；
            ``"lots"``   / ``"手"`` → 强制 ×100；
            ``"auto"``（默认）→ 用整批 ratio 中位数判定。

    清洗流程:
      0. 成交量/额单位归一化 → 统一为 V=股, Amt=元
      1. 去除 OHLC 空行
      2. 数值类型转换（字符串→浮点数）
      3. 验证 high >= low，否则剔除
      4. 修正 high < open/close → high 取最大值
      5. 修正 low > open/close → low 取最小值
      6. 去除价格 ≤ 0 的记录
      7. 去除停牌日（volume=0 或 NaN）
      8. 按 (ts_code, trade_date) 去重
      9. 按日期升序排列

    参数:
        df: 原始日线 DataFrame（来自任意数据源）

    返回:
        清洗后的 DataFrame（不修改原始数据）
    """
    if df.empty:                                     # 空DataFrame直接返回
        return df

    df = df.copy()                                   # 复制一份，避免污染原始数据
    original_len = len(df)                           # 记录清洗前的行数

    # ==================== 步骤1: 关键列检查与数值类型转换 ====================
    ohlc_cols = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in ohlc_cols):
        logger.warning(f"数据缺失OHLC关键列: {[c for c in ohlc_cols if c not in df.columns]}")
        return pd.DataFrame()

    # 原始数据(特别是Baostock或部分外部源)可能是字符串类型
    # pd.to_numeric 将字符串转为浮点数，无效值变为 NaN
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 关键价格字段(OHLC)任一为空 → 该行无法使用，直接去除
    df = df.dropna(subset=ohlc_cols)
    if df.empty:
        return df

    # ==================== 步骤2: 成交量/额单位归一化 ====================
    #
    # 统一目标：volume = 股，amount = 元。
    #
    # **整批一次决策**：ratio 含前复权因子 f（随除权事件阶梯变化，A股常见 1~3），
    # 逐行拿阈值判断会让同一批数据里一部分行 ×100、另一部分不动，直接造成单位
    # 混用。这里只看整批 ratio 的**中位数**，阈值放在 100 量级、远离 f 的取值域。
    # 若数据源能显式声明 volume_unit，则完全跳过启发式。
    # ====================
    if all(c in df.columns for c in ("close", "volume", "amount")):
        mask_ok = (df["close"] > 0) & (df["volume"] > 0) & (df["amount"] > 0)
        declared = str(volume_unit).strip().lower()
        if mask_ok.any():
            ratio_med = float(
                (df.loc[mask_ok, "amount"] /
                 (df.loc[mask_ok, "close"] * df.loc[mask_ok, "volume"])).median()
            )

            if declared in ("lots", "手") or (
                    declared == "auto" and ratio_med >= LOTS_RATIO_THRESHOLD):
                # V手 → V股；amount 已是元，保持不动
                df["volume"] = df["volume"] * 100
                logger.info(
                    f"规范化整批 {len(df)} 条: V手→V股(×100)，整批 ratio 中位数={ratio_med:.2f}"
                )
            elif declared == "auto" and ratio_med <= OLD_FORMAT_RATIO_THRESHOLD:
                # 旧格式：V手 + Amt千元
                df["volume"] = df["volume"] * 100
                df["amount"] = df["amount"] * 1000
                logger.info(
                    f"规范化整批 {len(df)} 条旧格式: V×100, Amt×1000，ratio 中位数={ratio_med:.3f}"
                )

    # ==================== 步骤3: high >= low 验证 ====================
    # 最高价必须 ≥ 最低价，否则数据有误，直接剔除
    invalid_hl = df["high"] < df["low"]
    if invalid_hl.any():
        logger.warning(f"发现{invalid_hl.sum()}条high<low的异常数据，已剔除")
        df = df[~invalid_hl]

    # ==================== 步骤4: high >= open/close 修正 ====================
    # 如果 highest < open 或 highest < close，将 high 修正为 open/close 中的最大值
    # 小偏差可能是API数据本身的精度问题，直接修正比剔除更友好
    invalid_h = (df["high"] < df["open"]) | (df["high"] < df["close"])
    if invalid_h.any():
        df.loc[invalid_h, "high"] = df.loc[invalid_h, ["open", "close"]].max(axis=1)

    # ==================== 步骤5: low <= open/close 修正 ====================
    # 如果 low > open 或 low > close，将 low 修正为 open/close 中的最小值
    invalid_l = (df["low"] > df["open"]) | (df["low"] > df["close"])
    if invalid_l.any():
        df.loc[invalid_l, "low"] = df.loc[invalid_l, ["open", "close"]].min(axis=1)

    # ==================== 步骤6: 去除价格 <= 0 ====================
    # 正常股票价格必须为正数，价格为0或负数说明数据错误
    price_zero = (df["close"] <= 0) | (df["open"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0)
    if price_zero.any():
        logger.warning(f"发现{price_zero.sum()}条价格<=0的记录，已剔除")
        df = df[~price_zero]

    # ==================== 步骤7: 去除停牌日 ====================
    # 停牌日成交量为 0 或 NaN，这些交易日没有真实交易数据
    # 保留会导致回测计算失真，故剔除
    if "volume" in df.columns:
        suspended = (df["volume"].isna()) | (df["volume"] == 0)
        if suspended.any():
            logger.debug(f"剔除{suspended.sum()}条停牌数据")
            df = df[~suspended]

    # ==================== 步骤8: 日期格式归一化与去重 ====================
    # 统一 trade_date 为标准 8 位 YYYYMMDD 字符串格式（剔除 - / 空格等异构格式）
    if "trade_date" in df.columns:
        df["trade_date"] = (
            df["trade_date"]
            .astype(str)
            .str.replace("-", "", regex=False)
            .str.replace("/", "", regex=False)
            .str.strip()
            .str[:8]
        )
    # 防止多次数据拉取导致同一日期的重复记录
    if "ts_code" in df.columns and "trade_date" in df.columns:
        df = df.drop_duplicates(subset=["ts_code", "trade_date"])
    elif "trade_date" in df.columns:
        df = df.drop_duplicates(subset=["trade_date"])

    # ==================== 步骤9: 排序 ====================
    # 按日期升序排列，确保后续指标计算（如均线、MACD）的顺序正确
    if "trade_date" in df.columns:
        df = df.sort_values("trade_date").reset_index(drop=True)

    # 记录清洗统计
    cleaned_len = len(df)
    if original_len != cleaned_len:
        logger.info(f"数据清洗: {original_len} -> {cleaned_len} 条 "
                     f"(移除{original_len - cleaned_len}条)")

    return df  # 返回清洗后的数据
