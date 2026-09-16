"""输入校验工具 - 统一验证股票代码、日期、金额等"""
import re
from core.exceptions import InvalidStockCodeError, InvalidDateError, ValidationError


# 股票代码正则：6位数字 + .SH/.SZ/.BJ
_STOCK_CODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# 日期正则：8位数字 YYYYMMDD
_DATE_PATTERN = re.compile(r"^\d{8}$")


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
