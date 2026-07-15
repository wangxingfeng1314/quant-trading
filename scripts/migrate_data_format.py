"""数据格式迁移脚本

问题诊断：
数据库中 daily_price 表的 volume(成交量) 和 amount(成交额) 存在三种格式混用：
  旧格式:   V=手(100股), Amt=千元(1000元),  判定: Amt/(C*V) ≈ 0.1
  新格式:   V=股,        Amt=元,             判定: Amt/(C*V) ≈ 1.0
  混合格式: V=手,        Amt=元,             判定: Amt/(C*V) ≈ 100

迁移目标：统一为 新格式 (V=股, Amt=元)
"""
import sys
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from data.storage import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def diagnose():
    """打印数据格式分布"""
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN amount * 1.0 / (close * volume) < 0.15 THEN 1 ELSE 0 END) as old_fmt,
                SUM(CASE WHEN amount * 1.0 / (close * volume) BETWEEN 0.5 AND 1.5 THEN 1 ELSE 0 END) as new_fmt,
                SUM(CASE WHEN amount * 1.0 / (close * volume) > 1.5 THEN 1 ELSE 0 END) as mixed_fmt
            FROM daily_price 
            WHERE close > 0 AND volume > 0 AND amount > 0
        """)
        r = cur.fetchone()
        total = r[0]
        logger.info(f"总记录: {total}")
        logger.info(f"  旧格式(V手/Amt千元, ratio≈0.1):   {r[1]:>8} ({r[1]/total*100:5.1f}%)")
        logger.info(f"  新格式(V股/Amt元,   ratio≈1.0):   {r[2]:>8} ({r[2]/total*100:5.1f}%)")
        logger.info(f"  混合格式(V手/Amt元, ratio≈100):   {r[3]:>8} ({r[3]/total*100:5.1f}%)")


def migrate():
    """执行数据迁移，统一为 V=股, Amt=元"""
    logger.info("=" * 60)
    logger.info("开始数据格式迁移...")

    with get_conn() as conn:
        # 1. 旧格式 → 新格式: V手→V股 (×100), Amt千元→Amt元 (×1000)
        cur = conn.execute("""
            UPDATE daily_price SET
                volume = ROUND(volume * 100, 0),
                amount = ROUND(amount * 1000, 2)
            WHERE close > 0 AND volume > 0 AND amount > 0
              AND amount * 1.0 / (close * volume) < 0.15
        """)
        logger.info(f"旧格式→新格式: {cur.rowcount} 条 (V×100, Amt×1000)")

        # 2. 混合格式 → 新格式: V手→V股 (×100), Amt不变
        cur = conn.execute("""
            UPDATE daily_price SET
                volume = ROUND(volume * 100, 0)
            WHERE close > 0 AND volume > 0 AND amount > 0
              AND amount * 1.0 / (close * volume) > 1.5
        """)
        logger.info(f"混合格式→新格式: {cur.rowcount} 条 (V×100, Amt不变)")

        # 3. 新格式不动

        conn.commit()

    logger.info("数据格式迁移完成！")


def verify():
    """验证迁移结果"""
    logger.info("=" * 60)
    logger.info("验证迁移结果...")

    with get_conn() as conn:
        # 检查异常数据比例
        cur = conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN amount * 1.0 / (close * volume) < 0.15 THEN 1 ELSE 0 END) as old_fmt,
                SUM(CASE WHEN amount * 1.0 / (close * volume) BETWEEN 0.5 AND 1.5 THEN 1 ELSE 0 END) as new_fmt,
                SUM(CASE WHEN amount * 1.0 / (close * volume) > 1.5 THEN 1 ELSE 0 END) as mixed_fmt
            FROM daily_price 
            WHERE close > 0 AND volume > 0 AND amount > 0
        """)
        r = cur.fetchone()
        total = r[0]
        logger.info(f"总记录: {total}")
        logger.info(f"  异常旧格式(≈0.1): {r[1]:>8} ({r[1]/total*100:5.1f}%)")
        logger.info(f"  正常(≈1.0):       {r[2]:>8} ({r[2]/total*100:5.1f}%)")
        logger.info(f"  异常混合格式(≈100): {r[3]:>8} ({r[3]/total*100:5.1f}%)")

        # 抽样验证
        logger.info("--- 抽样验证 ---")
        for ts_code in ["000001.SZ", "600519.SH", "300192.SZ"]:
            cur = conn.execute("""
                SELECT trade_date, close, volume, amount,
                       amount * 1.0 / (close * volume) as ratio
                FROM daily_price 
                WHERE ts_code = ? AND close > 0 AND volume > 0 AND amount > 0
                ORDER BY trade_date DESC LIMIT 3
            """, (ts_code,))
            rows = cur.fetchall()
            for r in rows:
                logger.info(f"  {ts_code} {r[0]}: C={r[1]:.2f} V={r[2]:>10,.0f} Amt={r[3]:>12,.0f} ratio={r[4]:.4f}")

    logger.info("验证完成！")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="迁移日线数据格式 (统一V=股, Amt=元)")
    parser.add_argument("--dry-run", action="store_true", help="仅诊断，不执行迁移")
    args = parser.parse_args()

    diagnose()

    if not args.dry_run:
        migrate()
        verify()
    else:
        logger.info("--dry-run 模式，未执行迁移")
