"""政策文件归集系统 CLI。

用法示例：
  python -m policy_collector.cli init-db
  python -m policy_collector.cli demo                 # 离线样例跑通全流程（推荐先跑）
  python -m policy_collector.cli sources              # 查看来源
  python -m policy_collector.cli run --source ndrc_zcwj --prefer rule
  python -m policy_collector.cli ingest --url <政策页URL> --source ndrc_zcwj
  python -m policy_collector.cli query --category 保障类 --keyword 投资
  python -m policy_collector.cli query --review pending
  python -m policy_collector.cli audit --id 3 --action confirm
  python -m policy_collector.cli export --format csv --out policies.csv
  python -m policy_collector.cli stats
  python -m policy_collector.cli schedule --source ndrc_zcwj --interval 60
  python -m policy_collector.cli web --open        # 启动本地 Web 管理界面
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .config import AppConfig, DEFAULT_CLASSIFY, DEFAULT_CONFIG, DEFAULT_SOURCES
from .db import Database
from .pipeline import Pipeline
from .scheduler import Scheduler


def _cfg(args: argparse.Namespace) -> AppConfig:
    return AppConfig.load(
        config_path=args.config,
        sources_path=args.sources,
        classify_path=args.classify,
    )


def cmd_init_db(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    n = pipe.sync_sources()
    print(f"数据库就绪: {cfg.db_path}")
    print(f"已同步来源 {n} 个：{', '.join(cfg.sources.keys())}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    print(f"{'名称':<16}{'站点':<18}{'地区':<6}{'栏目分类':<10}{'启用':<4}最近检查")
    for s in db.list_sources():
        print(f"{s['name']:<16}{s['site']:<18}{s['region']:<6}{s['category']:<10}"
              f"{'是' if s['enabled'] else '否':<4}{s['last_checked_at'] or '-'}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    stats = pipe.run_demo()
    print("离线样例演示完成（采集→解析→分类→去重→入库）：")
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    print("\n查询结果：")
    for p in pipe.db.query_policies(limit=20):
        mark = "【待复核】" if p["need_review"] else ""
        cat = (p["category_names"] or "-")[:24]
        print(f"  #{p['id']} v{p['version']} [{cat}] {p['title'][:40]}{mark} ({p['region']})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    pipe = Pipeline(cfg)
    stats = pipe.run_source(args.source, prefer=args.prefer, limit=args.limit)
    print(f"来源 [{args.source}] 运行完成：")
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    src = cfg.sources.get(args.source)
    if src is None:
        print(f"来源不存在: {args.source}（先运行 init-db / 检查 config/sources.yaml）")
        return 2
    pipe = Pipeline(cfg)
    stats = pipe.ingest_url(src, args.url, prefer=args.prefer)
    print(f"入库结果：{json.dumps(stats.to_dict(), ensure_ascii=False)}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    rows = db.query_policies(
        region=args.region or "",
        category=args.category or "",
        keyword=args.keyword or "",
        review_status=args.review or "",
        limit=args.limit,
    )
    if not rows:
        print("（无匹配记录）")
        return 0
    print(f"共 {len(rows)} 条：")
    for p in rows:
        flag = "待复核" if p["need_review"] else "已入库"
        cat = (p["category_names"] or "-")[:24]
        print(f"  #{p['id']} v{p['version']} [{flag}] {cat} | {p['title'][:50]}（{p['region'] or '-'}）")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """人工复核确认：confirm(确认采纳)/adjust(调整分类)/reject(剔除)。"""
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    p = db.get_policy(args.id)
    if p is None:
        print(f"政策 #{args.id} 不存在")
        return 2
    if args.action == "confirm":
        db.update_policy(args.id, review_status="confirmed", need_review=0)
    elif args.action == "adjust":
        new_cat = args.category or ""
        names = {"guide": "引导类", "access": "准入类", "guarantee": "保障类", "incentive": "激励约束类"}
        cat_names = ",".join(names.get(c, c) for c in new_cat.split(",") if c)
        db.update_policy(args.id, category=new_cat, category_names=cat_names,
                         review_status="adjusted", need_review=0)
    elif args.action == "reject":
        db.update_policy(args.id, review_status="rejected", need_review=0)
    else:
        print(f"未知动作: {args.action}")
        return 2
    print(f"政策 #{args.id} 已执行 [{args.action}]，review_status 已更新。")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    rows = db.query_policies(limit=args.limit)
    out = args.out or str(Path(cfg.data_dir.parent) / "export_policies.csv")
    if args.format == "csv":
        fieldnames = ["id", "version", "title", "wenhao", "issuing_authority", "page_date",
                      "doc_date", "region", "site", "doc_type", "category", "category_names",
                      "is_investment_policy", "need_review", "review_status", "model_version", "page_url"]
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    elif args.format == "json":
        Path(out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print("--format 仅支持 csv/json")
        return 2
    print(f"已导出 {len(rows)} 条 → {out}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    db = Database(cfg.db_path)
    total = db.query_policies(limit=100000)
    need_review = sum(1 for p in total if p["need_review"])
    print(f"政策总数: {len(total)}（含版本）| 待复核: {need_review}")
    from collections import Counter
    by_cat = Counter(p["category_names"] or "未分类" for p in total)
    for k, v in by_cat.most_common():
        print(f"  {k}: {v}")
    print("\n最近运行日志：")
    for r in db.list_runs(limit=5):
        print(f"  [{r['started_at']}] {r['run_id']} {r['kind']}/{r['status']} "
              f"summary={r['summary'][:120]}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    sched = Scheduler(cfg, interval_minutes=args.interval)
    sched.run_forever(source_names=args.source.split(",") if args.source else None, prefer=args.prefer)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """启动本地 Web 管理界面（需 flask 依赖）。"""
    try:
        from .webapp import create_app
    except ImportError:
        print("缺少 Flask：先执行 pip install -r requirements.txt")
        return 2
    cfg = _cfg(args)
    app = create_app(cfg)
    url = f"http://{args.host}:{args.port}"
    if args.open:
        import threading
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url + "/")).start()
    print(f"政策文件归集系统 Web 界面: {url}  (Ctrl+C 退出)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="policy-collector", description="政策文件归集系统 v" + __version__)
    parser.add_argument("--config", default=None, help=f"运行配置 (默认 {DEFAULT_CONFIG})")
    parser.add_argument("--sources", default=None, help=f"来源配置 (默认 {DEFAULT_SOURCES})")
    parser.add_argument("--classify", default=None, help=f"分类口径配置 (默认 {DEFAULT_CLASSIFY})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库并同步来源")
    sub.add_parser("sources", help="列出采集来源")
    sub.add_parser("demo", help="离线样例跑通全流程（无需联网/Key）")

    p = sub.add_parser("run", help="运行指定来源的增量采集入库")
    p.add_argument("--source", required=True)
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("ingest", help="手工入库单篇 URL")
    p.add_argument("--url", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])

    p = sub.add_parser("query", help="按条件查询政策")
    p.add_argument("--region", default="")
    p.add_argument("--category", default="")
    p.add_argument("--keyword", default="")
    p.add_argument("--review", default="", help="pending/confirmed/rejected")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("audit", help="人工复核：confirm/adjust/reject")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--action", choices=["confirm", "adjust", "reject"], required=True)
    p.add_argument("--category", default="", help="adjust 时的新分类，如 guarantee,incentive")

    p = sub.add_parser("export", help="导出政策数据")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--out", default="")
    p.add_argument("--limit", type=int, default=100000)

    sub.add_parser("stats", help="统计与最近运行日志")

    p = sub.add_parser("schedule", help="按间隔循环运行来源")
    p.add_argument("--source", default="", help="逗号分隔；缺省跑全部启用来源")
    p.add_argument("--interval", type=int, default=0, help="间隔分钟（缺省用配置）")
    p.add_argument("--prefer", default="llm", choices=["llm", "rule"])

    p = sub.add_parser("web", help="启动本地 Web 管理界面（需 flask）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = {
        "init-db": cmd_init_db,
        "sources": cmd_sources,
        "demo": cmd_demo,
        "run": cmd_run,
        "ingest": cmd_ingest,
        "query": cmd_query,
        "audit": cmd_audit,
        "export": cmd_export,
        "stats": cmd_stats,
        "schedule": cmd_schedule,
        "web": cmd_web,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
