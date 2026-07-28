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
#   用于检测AKShare不同版本返回的数据格式并自动修正
# ============================================================
OLD_FORMAT_RATIO_THRESHOLD = 0.15    # 旧格式: V手, Amt千元 → ratio≈0.1, 阈值<0.15
MIXED_FORMAT_RATIO_THRESHOLD = 1.5   # 混合格式: V手, Amt元 → ratio≈100, 阈值>1.5


def clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    """清洗日线数据，确保数据质量

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

    # ==================== 步骤0: 成交量/额单位归一化 ====================
    #
    # 兼容 AKShare API 不同版本返回的三种数据格式：
    #   DataFrame 中 volume 和 amount 的单位可能不一致
    #
    # 检测方法：计算 ratio = amount / (close * volume)
    #   - 新格式（当前版本）: V=股, Amt=元, ratio ≈ 1.0
    #   - 旧格式:             V=手(100股), Amt=千元(1000元), ratio ≈ 0.1
    #   - 混合格式:           V=手(100股), Amt=元, ratio ≈ 100
    #
    # 修正策略:
    #   - ratio < OLD_FORMAT_RATIO_THRESHOLD(0.15)    → 旧格式: V×100(手→股), Amt×1000(千元→元)
    #   - ratio > MIXED_FORMAT_RATIO_THRESHOLD(1.5)   → 混合格式: V×100(手→股), Amt不变
    #   - 之间                                            → 新格式: 无需处理
    #
    # 注意: 此检测只对有成交量的样本有效(close>0, volume>0, amount>0)
    # ====================
    if "close" in df.columns and "volume" in df.columns and "amount" in df.columns:
        # 筛选有效样本（价格和量都大于0）
        mask_ok = (df["close"] > 0) & (df["volume"] > 0) & (df["amount"] > 0)
        if mask_ok.any():
            # 计算归一化比率
            ratio = df.loc[mask_ok, "amount"] / (df.loc[mask_ok, "close"] * df.loc[mask_ok, "volume"])

            # ----- 旧格式检测: ratio ≈ 0.1 -----
            # V在"手"单位下数值偏小 → volume × 100 转为"股"
            # Amt在"千元"单位下数值偏小 → amount × 1000 转为"元"
            is_old = ratio < OLD_FORMAT_RATIO_THRESHOLD
            if is_old.any():
                idx = ratio[is_old].index
                df.loc[idx, "volume"] *= 100
                df.loc[idx, "amount"] *= 1000
                logger.info(f"规范化{len(idx)}条旧格式数据: V手→V股(×100), Amt千元→Amt元(×1000)")

            # ----- 混合格式检测: ratio ≈ 100 -----
            # V在"手"单位下数值偏小 → volume × 100 转为"股"
            # Amt已经是"元"单位 → 不变
            is_mixed = ratio > MIXED_FORMAT_RATIO_THRESHOLD
            if is_mixed.any():
                idx = ratio[is_mixed].index
                df.loc[idx, "volume"] *= 100
                logger.info(f"规范化{len(idx)}条混合格式数据: V手→V股(×100), Amt不变")

    # ==================== 步骤1: 去除空行 ====================
    # 关键价格字段(OHLC)任一为空 → 该行无法使用，直接去除
    df = df.dropna(subset=["open", "high", "low", "close"])

    # ==================== 步骤2: 数值类型转换 ====================
    # 原始数据(特别是Baostock返回的)可能是字符串类型
    # pd.to_numeric 将字符串转为浮点数，无效值变为 NaN
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

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
    price_zero = (df["close"] <= 0) | (df["open"] <= 0)
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

    # ==================== 步骤8: 去重 ====================
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
