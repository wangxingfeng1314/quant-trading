"""策略注册表 — 自动发现

通过 importlib 自动扫描 strategies/ 目录下的所有策略文件，
无需手动维护 import 和注册表。添加新策略只需：
  1. 在 strategies/ 下新建 .py 文件，继承 BaseStrategy
  2. 重启即可自动识别
"""
import logging
import importlib
import inspect
import pkgutil
from pathlib import Path

from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# 策略注册表：名称 -> 策略类（自动填充）
STRATEGY_REGISTRY = {}

# 此文件所在目录（strategies/）
_PACKAGE_DIR = Path(__file__).parent


def _discover_strategies():
    """自动发现 strategies/ 目录下所有 BaseStrategy 子类

    扫描逻辑：
      1. 遍历 strategies/ 下所有 .py 文件（不含 __init__ 和 base）
      2. import 每个模块
      3. 查找模块中 BaseStrategy 的子类
      4. 按 name 属性注册到 STRATEGY_REGISTRY
    """
    discovered = {}

    # 获取 strategies 包自身
    import strategies as pkg

    for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        # 跳过 __init__、base 和包目录
        if modname.startswith("_") or ispkg:
            continue
        try:
            # 动态导入模块
            module = importlib.import_module(f"strategies.{modname}")

            # 查找模块中所有 BaseStrategy 子类
            for name, cls in inspect.getmembers(module, inspect.isclass):
                if (issubclass(cls, BaseStrategy) and cls is not BaseStrategy
                        and hasattr(cls, "name") and cls.name):
                    discovered[cls.name] = cls
                    logger.debug(f"自动发现策略: {cls.name} ({modname}.py)")
        except Exception as e:
            logger.warning(f"加载策略模块 {modname}.py 失败: {e}")

    if not discovered:
        logger.warning("未发现任何策略，请检查 strategies/ 目录")

    return discovered


def get_strategy(name: str):
    """根据名称获取策略类"""
    if not STRATEGY_REGISTRY:
        _discover_and_register()
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"未知策略: {name}, 可选: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> list:
    """列出所有策略"""
    if not STRATEGY_REGISTRY:
        _discover_and_register()
    return [
        {"name": name, "desc": cls.description, "params": cls.param_schema}
        for name, cls in STRATEGY_REGISTRY.items()
    ]


def _discover_and_register():
    """执行自动发现并填充全局注册表"""
    global STRATEGY_REGISTRY
    STRATEGY_REGISTRY = _discover_strategies()

    count = len(STRATEGY_REGISTRY)
    names = ", ".join(STRATEGY_REGISTRY.keys())
    logger.info(f"策略自动发现完成: 共 {count} 个 ({names})")


# 模块加载时自动发现（保持与旧代码兼容：import 后即可使用 STRATEGY_REGISTRY）
_discover_and_register()
