"""一次性修复历史日线数据的成交量单位断层。

背景
----
`clean_daily` 旧实现逐行用 ``ratio = amount/(close*volume)`` 与阈值 1.5 判定
成交量单位。但 ``close`` 是**前复权价**、`amount/volume` 是**未复权成交原价**，
两者相差随除权事件阶梯变化的复权因子 f，于是同一批次内会出现「一部分行 ×100、
另一部分不动」，或数据源切换后整段单位不一致。

判据
----
``vr = amount / (volume * close)``（等价于每股成交均价 / 前复权收盘价）：

* volume 为「股」、amount 为「元」→ vr ≈ f，A股常见 1~3（ETF 可能 0.5）
* volume 为「手」、amount 为「元」→ vr ≈ 100 * f，通常 > 20

因此 ``vr > LOTS_THRESHOLD(20)`` 的行判定为「手」，统一 ×100 转成「股」。
阈值 20 远离 f 的取值域，不会把正常复权因子误判成单位问题；真实成交量放大
（如 ETF 巨量申赎）因为 amount 同步变化，vr 仍 ≈1，也不会被误判。

用法
----
    py scripts/repair_volume_units.py                # 只扫描并打印报告（dry-run）
    py scripts/repair_volume_units.py --apply        # 备份数据库后执行修复
    py scripts/repair_volume_units.py --apply --db path/to/other.db

安全性：默认不写库；``--apply`` 前用 SQLite 在线备份 API 生成完整快照；
所有 UPDATE 在单个事务内完成；脚本可重复执行（修复后 vr≈1，不会二次 ×100）。
"""
import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DB_PATH  # noqa: E402

LOTS_THRESHOLD = 20.0

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def scan(conn: sqlite3.Connection) -> list:
    """扫描所有标的，返回需要修复的清单。"""
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM daily_price ORDER BY ts_code"
    ).fetchall()]

    findings = []
    for code in codes:
        df = pd.read_sql(
            "SELECT trade_date, close, volume, amount FROM daily_price "
            "WHERE ts_code = ? ORDER BY trade_date", conn, params=(code,)
        )
        if df.empty:
            continue
        valid = (df["close"] > 0) & (df["volume"] > 0) & (df["amount"] > 0)
        if not valid.any():
            continue
        vr = pd.Series(float("nan"), index=df.index)
        vr.loc[valid] = (df.loc[valid, "amount"] /
                         (df.loc[valid, "volume"] * df.loc[valid, "close"]))
        lots = vr > LOTS_THRESHOLD
        n_lots = int(lots.sum())
        if n_lots == 0:
            continue

        first = str(df.loc[lots, "trade_date"].min())
        last = str(df.loc[lots, "trade_date"].max())
        findings.append({
            "ts_code": code,
            "rows": len(df),
            "lots_rows": n_lots,
            "first": first,
            "last": last,
            "vr_lots": round(float(vr[lots].median()), 2),
            "vr_normal": round(float(vr[valid & ~lots].median()), 3)
                         if int((valid & ~lots).sum()) else None,
            "trade_dates": df.loc[lots, "trade_date"].astype(str).tolist(),
        })
    return findings


def backup_database(src: Path) -> Path:
    """用 SQLite 在线备份 API 生成一致快照（兼容 WAL 模式）。"""
    backup_dir = src.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"quant_before_volume_repair_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dst)) as target:
        source.backup(target)
    logger.info(f"数据库已备份 → {dst}")
    return dst


def main():
    ap = argparse.ArgumentParser(description="修复日线成交量单位（手→股）断层")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只报告）")
    ap.add_argument("--db", type=str, default="", help="指定数据库路径（默认用项目配置）")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        logger.error(f"数据库不存在: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        findings = scan(conn)
        total_rows = int(conn.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0])
        total_codes = int(conn.execute(
            "SELECT COUNT(DISTINCT ts_code) FROM daily_price").fetchone()[0])
        print()
        print("=" * 78)
        print(f"数据库: {db_path}  |  {total_codes} 只标的 / {total_rows} 行")
        print("=" * 78)
        if not findings:
            print("未发现成交量单位异常（无需修复）。")
            return 0

        print(f"发现 {len(findings)} 只标的存在「手」单位的行情段：\n")
        print(f"{'代码':<12}{'总行数':>7}{'待修行数':>9}{'起始':>10}{'结束':>10}"
              f"{'vr(手)':>10}{'vr(正常)':>10}")
        for f in findings:
            print(f"{f['ts_code']:<12}{f['rows']:>7}{f['lots_rows']:>9}"
                  f"{f['first']:>10}{f['last']:>10}{f['vr_lots']:>10}"
                  f"{('%.3f' % f['vr_normal']) if f['vr_normal'] is not None else '-':>10}")
        total_fix = sum(f["lots_rows"] for f in findings)
        print(f"\n合计待修复 {total_fix} 行（volume ×100）")

        if not args.apply:
            print("\n[DRY-RUN] 未写入。确认无误后加 --apply 执行（会自动备份数据库）。")
            return 0

        backup_database(db_path)
        try:
            for f in findings:
                dates = f["trade_dates"]
                for i in range(0, len(dates), 400):          # 规避 SQLite 变量上限
                    chunk = dates[i:i + 400]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        f"UPDATE daily_price SET volume = volume * 100 "
                        f"WHERE ts_code = ? AND trade_date IN ({placeholders})",
                        [f["ts_code"]] + chunk,
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("修复失败，事务已回滚")
            return 1

        after = scan(conn)
        print(f"\n修复完成：写入 {total_fix} 行；复扫剩余异常标的 "
              f"{len(after)} 只 / {sum(x['lots_rows'] for x in after)} 行")
        for f in findings:
            row = conn.execute(
                "SELECT close, volume, amount FROM daily_price WHERE ts_code = ? "
                "ORDER BY trade_date DESC LIMIT 1", (f["ts_code"],)
            ).fetchone()
            if row and row[1] > 0 and row[0] > 0:
                print(f"  {f['ts_code']} 最新日 vr = {row[2] / (row[1] * row[0]):.3f}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
