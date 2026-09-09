#!/usr/bin/env python3
"""浙江 zjfgw_gsgg 全量入库 + 双轮连续性验证（隔离数据目录，不影响正式数据）。

对应 README「尚需完成的验收」第 2 条：浙江 387 条全量正文入库与双轮连续性运行。

验证目标：
  轮 1  全量翻页发现 -> 全部候选真实入库。统计：入库数 / 文号缺失率 /
       附件下载成功率与解析覆盖 / 规则排除数 / 运行状态(ok|partial|failed)。
  轮 2  同一数据目录再跑一轮，验证：
        - 幂等：无新增入库(ingested≈0)，重复走 duplicate_skip(duplicates>0)；
        - 失败补采：轮 1 failed 的候选若站点恢复，本轮应转 processed；
        - 元数据补齐：metadata_backfill 留痕出现时说明空字段被补齐。

用法：
  python scripts/zj_full_ingest_check.py                 # 默认：全新隔离库 /tmp/pc_zj_full，两轮
  python scripts/zj_full_ingest_check.py --rounds 1       # 只跑一轮（冒烟/复验）
  python scripts/zj_full_ingest_check.py --limit 600      # 每轮最多处理的候选数（默认 600 > 387）
  python scripts/zj_full_ingest_check.py --keep           # 复用已存在的隔离库续跑（不重建）

产物：<data-dir>/report.json（结构化汇总）。数据与原件仅写入 <data-dir>，绝不触碰项目 data/。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SOURCE = "zjfgw_gsgg"  # 浙江行政规范性文件（unitbuild 动态列表全量翻页）


def db_stats(db_path: Path) -> dict:
    """从隔离库统计入库质量维度（只读）。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out: dict = {}

    row = con.execute(
        "SELECT id FROM source_configs WHERE name=?", (SOURCE,)
    ).fetchone()
    src_id = row["id"] if row else None
    out["source_found"] = src_id is not None

    # 采集记录状态分布（含被规则排除/失败项，反映全量处理结果）
    if src_id:
        out["fetch_by_status"] = {
            r["status"]: r["n"]
            for r in con.execute(
                "SELECT status, COUNT(*) n FROM fetch_records WHERE source_id=? GROUP BY status",
                (src_id,),
            )
        }
        out["fetch_total"] = sum(out["fetch_by_status"].values())

    # 当前版本政策（剔除已 rejected 的历史/当前版本）
    out["policy_current"] = con.execute(
        """SELECT COUNT(*) FROM policies p
           WHERE version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
             AND review_status!='rejected'"""
    ).fetchone()[0]
    out["review_status"] = {
        r["review_status"]: r["n"]
        for r in con.execute(
            """SELECT review_status, COUNT(*) n FROM policies p
               WHERE version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
               GROUP BY review_status"""
        )
    }
    # 文号缺失率（只算当前版本）
    cur = con.execute(
        """SELECT COUNT(*) n,
                  SUM(CASE WHEN wenhao='' OR wenhao IS NULL THEN 1 ELSE 0 END) no_wenhao
           FROM policies p
           WHERE version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
             AND review_status!='rejected'"""
    ).fetchone()
    out["wenhao_missing"] = cur["no_wenhao"]
    out["wenhao_missing_rate"] = round(cur["no_wenhao"] / cur["n"], 3) if cur["n"] else None

    # 附件维度：格式分布 / 解析状态分布 / 失败原因 TOP
    out["attachments"] = {"total": con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]}
    out["attachments"]["by_fmt"] = {
        r["fmt"] or "(未知)": r["n"]
        for r in con.execute("SELECT fmt, COUNT(*) n FROM attachments GROUP BY fmt ORDER BY n DESC")
    }
    out["attachments"]["by_parse_status"] = {
        r["parse_status"]: r["n"]
        for r in con.execute("SELECT parse_status, COUNT(*) n FROM attachments GROUP BY parse_status ORDER BY n DESC")
    }
    top_err = con.execute(
        """SELECT substr(COALESCE(error,'(空)'),1,60) e, COUNT(*) n FROM attachments
           WHERE error!='' GROUP BY e ORDER BY n DESC LIMIT 5"""
    ).fetchall()
    out["attachments"]["top_errors"] = [dict(r) for r in top_err]

    # 运行日志（每轮 summary）
    out["runs"] = [
        {"id": r["run_id"], "status": r["status"], "summary": json.loads(r["summary"] or "{}"),
         "started_at": r["started_at"], "finished_at": r["finished_at"], "note": r["note"]}
        for r in con.execute("SELECT * FROM run_logs ORDER BY id")
    ]
    con.close()
    return out


def snapshot(db_path: Path, label: str) -> None:
    print(f"\n===== 快照[{label}] =====")
    print(json.dumps(db_stats(db_path), ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="/tmp/pc_zj_full", help="隔离数据目录（默认 /tmp/pc_zj_full）")
    ap.add_argument("--rounds", type=int, default=2, help="连续性运行轮数（默认 2）")
    ap.add_argument("--limit", type=int, default=600, help="每轮最多处理候选数（默认 600>387）")
    ap.add_argument("--keep", action="store_true", help="复用已有隔离库（默认重建以确保从零验证）")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    db_path = data_dir / "policy.db"
    # 安全护栏：拒绝指向项目正式数据目录
    project_root = Path(__file__).resolve().parent.parent
    if data_dir == (project_root / "data") or str(data_dir).startswith(str(project_root / "data")):
        print(f"错误：隔离目录 {data_dir} 位于项目 data/ 之下，会污染正式数据。请换用 /tmp 或其它目录。", file=sys.stderr)
        return 2
    if not args.keep and db_path.exists():
        import shutil
        print(f"重建隔离目录 {data_dir}（--keep 可复用续跑）")
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 关键：在 AppConfig.load() 之前设置数据目录环境变量
    os.environ["POLICY_DATA_DIR"] = str(data_dir)
    from policy_collector.config import AppConfig
    from policy_collector.pipeline import Pipeline

    cfg = AppConfig.load()
    if SOURCE not in cfg.sources:
        print(f"错误：来源 {SOURCE} 不在 config/sources.yaml", file=sys.stderr)
        return 2
    print(f"隔离数据目录 : {data_dir}")
    print(f"数据库        : {db_path}")
    print(f"来源          : {SOURCE}（max_pages={cfg.sources[SOURCE].max_pages}，prefer=rule 规则分类）")
    print(f"轮数          : {args.rounds}，每轮 limit={args.limit}\n")

    overall = {"source": SOURCE, "data_dir": str(data_dir), "rounds": [], "final": {}}
    pipe = Pipeline(cfg)  # 自动建表（含 migrate 补列）
    try:
        for i in range(1, args.rounds + 1):
            t0 = time.monotonic()
            print(f"\n########## 轮 {i}/{args.rounds} 开始 ##########", flush=True)
            stats = pipe.run_source(SOURCE, prefer="rule", limit=args.limit)
            elapsed = round(time.monotonic() - t0, 1)
            d = stats.to_dict()
            d["elapsed_seconds"] = elapsed
            overall["rounds"].append({"round": i, **d})
            print(f"轮 {i} 完成（{elapsed}s）: {json.dumps(d, ensure_ascii=False)}", flush=True)
            snapshot(db_path, f"轮 {i} 之后")
            # 每轮结束立即写一次 report.json，中断也能保留已跑轮次
            overall["final"] = db_stats(db_path)
            (data_dir / "report.json").write_text(
                json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        pipe.close()

    # ---- 验收判定 ----
    r1 = overall["rounds"][0]
    f = overall["final"]
    veredict = {}
    ok = True
    if args.rounds >= 1:
        veredict["r1_processed"] = r1["ingested"] + r1["duplicates"] + r1["excluded"] + r1["failed"]
        veredict["r1_no_mass_failure"] = (r1["failed"] / max(1, r1["ingested"] + r1["failed"])) < 0.5
        ok &= veredict["r1_no_mass_failure"]
    if args.rounds >= 2:
        r2 = overall["rounds"][1]
        veredict["r2_idempotent_no_new_ingest"] = r2["ingested"] == 0
        veredict["r2_rescan_happened"] = r2["duplicates"] > 0
        # 失败补采：轮 1 failed 数 > 轮 2 failed 数 或 轮2 failed 集中在附件
        veredict["r2_failed_shrunk_or_stable"] = r2["failed"] <= r1["failed"]
        ok &= all([veredict["r2_idempotent_no_new_ingest"], veredict["r2_rescan_happened"]])
    veredict["overall_ok"] = ok
    overall["veredict"] = veredict
    (data_dir / "report.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n===== 验收判定 =====")
    print(json.dumps(veredict, ensure_ascii=False, indent=2))
    print(f"\n完整报告：{data_dir / 'report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
