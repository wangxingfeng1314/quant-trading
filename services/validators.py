"""输入校验工具 - 统一验证股票代码、日期、金额等"""
import re
from core.exceptions import InvalidStockCodeError, InvalidDateError, ValidationError


# 股票代码正则：6位数字 + .SH/.SZ/.BJ
_STOCK_CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# 日期正则：8位数字 YYYYMMDD
_DATE_PATTERN = re.compile(r"^\d{8}$")


def normalize_stock_code(code: str) -> str:
    """智能归一化股票/ETF代码为标准格式 (如 600519.SH)

    支持格式兼容：
      - 纯6位数字: "600519" -> "600519.SH", "000001" -> "000001.SZ", "830001" -> "830001.BJ"
      - 前缀小写/大写: "sh600519" -> "600519.SH", "SZ000001" -> "000001.SZ", "bj920002" -> "920002.BJ"
      - 后缀小写: "600519.sh" -> "600519.SH"
      - 常见分隔符: "600519-SH" / "600519_SH" / "600519。SH" -> "600519.SH"
    """
    if not code:
        return ""
    s = str(code).strip().upper().replace("。", ".").replace("-", ".").replace("_", ".")

    # 1. 匹配类似 SH600519 / SZ000001 / BJ830001 的前缀格式
    m_prefix = re.match(r"^(SH|SZ|BJ)(\d{6})$", s)
    if m_prefix:
        return f"{m_prefix.group(2)}.{m_prefix.group(1)}"

    # 2. 匹配已有点号后缀的格式
    if "." in s:
        parts = s.split(".")
        if len(parts) == 2 and len(parts[0]) == 6 and parts[1] in ("SH", "SZ", "BJ"):
            return f"{parts[0]}.{parts[1]}"

    # 3. 匹配纯6位数字格式，智能推断市场后缀
    m_digits = re.match(r"^(\d{6})$", s)
    if m_digits:
        num = m_digits.group(1)
        if num.startswith(("60", "68", "51", "58", "56", "90")):
            return f"{num}.SH"
        elif num.startswith(("00", "30", "15", "16", "20")):
            return f"{num}.SZ"
        elif num.startswith(("43", "83", "87", "92")):
            return f"{num}.BJ"
        # 默认按沪市
        return f"{num}.SH"

    return s


def validate_stock_code(code: str) -> str:
    """校验股票代码格式

    Args:
        code: 股票代码 e.g. "000001.SZ"

    Returns:
        校验通过的代码

    Raises:
        InvalidStockCodeError: 格式不正确
    """
    if not code or not _STOCK_CODE_PATTERN.match(code):
        raise InvalidStockCodeError(code)
    return code


def validate_date(date_str: str) -> str:
    """校验日期格式（YYYYMMDD）

    Args:
        date_str: 日期字符串 e.g. "20240101"

    Returns:
        校验通过的日期字符串

    Raises:
        InvalidDateError: 格式不正确
    """
    from datetime import datetime
    if not date_str or not _DATE_PATTERN.match(date_str):
        raise InvalidDateError(date_str)
    # 日历与闰年合法性检查
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        raise InvalidDateError(date_str)
    if not (1990 <= dt.year <= 2100):
        raise InvalidDateError(date_str)
    return date_str


def validate_date_range(start_date: str, end_date: str) -> tuple:
    """校验日期范围（start <= end）

    Returns:
        (start_date, end_date) 元组

    Raises:
        InvalidDateError: 格式错误或 start > end
    """
    start = validate_date(start_date)
    end = validate_date(end_date)
    if start > end:
        raise InvalidDateError(f"开始日期 {start} 晚于结束日期 {end}")
    return start, end


def validate_capital(capital: float) -> float:
    """校验资金金额

    Args:
        capital: 资金金额

    Returns:
        校验通过的资金金额

    Raises:
        ValidationError: 金额无效
    """
    if not isinstance(capital, (int, float)):
        raise ValidationError(f"资金必须是数字: {capital}", field="capital")
    if capital < 1000:
        raise ValidationError(f"资金不能少于 1000 元: {capital}", field="capital")
    if capital > 1e9:
        raise ValidationError(f"资金超出合理范围: {capital}", field="capital")
    return float(capital)


def validate_strategy_name(name: str, registry: dict) -> str:
    """校验策略名称是否存在于注册表

    Args:
        name: 策略名称
        registry: STRATEGY_REGISTRY 字典

    Returns:
        校验通过的策略名称

    Raises:
        ValidationError: 策略不存在
    """
    if not name or name not in registry:
        raise ValidationError(f"策略不存在: {name}", field="strategy")
    return name
