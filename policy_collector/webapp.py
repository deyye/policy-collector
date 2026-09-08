"""政策文件归集系统 - 本地 Web 管理界面。

提供比命令行直观的操作入口：
  仪表盘 / 政策库(检索·分页·详情) / 人工复核(确认·调整·剔除) / 来源管理与运行 / 运行日志

启动（默认仅本机访问）：
    python -m policy_collector.cli web --port 8000 --open
或：
    python -m policy_collector.webapp --port 8000

依赖 Flask（requirements.txt 已含）。所有写操作仅作用于本机 SQLite 库。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from .config import AppConfig
from .db import Database
from .pipeline import Pipeline

CAT_CODES = {"guide": "引导类", "access": "准入类", "guarantee": "保障类", "incentive": "激励约束类"}

# 串行化"运行采集"，避免同一时刻多线程重复抓取同一来源
_RUN_LOCK = threading.Lock()
_REVIEW_STYLE = {
    "pending": "warn", "confirmed": "ok", "confirmed_auto": "ok",
    "adjusted": "ok", "rejected": "bad",
}


def _cat_label(code: str) -> str:
    return CAT_CODES.get(code, code)


def _policy_category_names(p: dict) -> list[str]:
    return [c for c in (p.get("category") or "").split(",") if c]


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig.load()
    app = Flask(__name__)
    app.secret_key = "policy-collector-local"  # 仅本地 flash 消息用，非安全边界
    app.cfg = cfg

    def db() -> Database:
        return Database(cfg.db_path)

    # 开箱即用：来源表为空时自动同步 sources.yaml（等价 CLI `init-db`），
    # 避免直接启动 web 后 /sources 页面无来源、无"运行"入口。
    _boot = Database(cfg.db_path)
    if not _boot.list_sources():
        try:
            n = Pipeline(cfg).sync_sources()
            print(f"[web] 来源表为空，已自动同步 {n} 个来源（{', '.join(cfg.sources)}）")
        except Exception as e:  # noqa: BLE001
            print(f"[web] 自动同步来源失败: {e}")
    _boot.close()

    # ---------------- 仪表盘 ----------------
    @app.route("/")
    def index():
        d = db()
        stats = d.dashboard()
        recent = d.query_policies(limit=8)
        runs = d.list_runs(limit=5)
        return render_template("index.html", stats=stats, recent=recent, runs=runs)

    # ---------------- 政策库 ----------------
    @app.route("/policies")
    def policies():
        q = request.args.get("q", "").strip()
        category = request.args.get("category", "").strip()
        region = request.args.get("region", "").strip()
        review = request.args.get("review", "").strip()
        page = max(request.args.get("page", 1, type=int), 1)
        per = 20
        d = db()
        rows = d.query_policies(region=region, category=category, keyword=q,
                                review_status=review, limit=per, offset=(page - 1) * per)
        has_more = len(rows) == per
        regions = sorted({r["region"] for r in d.query_policies(limit=2000) if r["region"]})
        return render_template(
            "policies.html", rows=rows, q=q, category=category, region=region, review=review,
            page=page, has_more=has_more, regions=regions, cat_codes=CAT_CODES, _cat_label=_cat_label,
        )

    @app.route("/policies/<int:pid>")
    def policy_detail(pid: int):
        d = db()
        p = d.get_policy(pid)
        if p is None:
            abort(404)
        versions = d.query_policies(keyword="", limit=2000)
        versions = [v for v in versions if v["policy_key"] == p["policy_key"]]
        attachments = d.list_attachments(pid)
        cats = _policy_category_names(p)
        return render_template(
            "policy.html", p=p, versions=versions, attachments=attachments, cats=cats,
            cat_codes=CAT_CODES, _cat_label=_cat_label, review_style=_REVIEW_STYLE.get(p["review_status"], ""),
        )

    # ---------------- 人工复核 ----------------
    @app.route("/policies/<int:pid>/review", methods=["POST"])
    def policy_review(pid: int):
        d = db()
        p = d.get_policy(pid)
        if p is None:
            abort(404)
        action = request.form.get("action", "")
        if action == "confirm":
            d.update_policy(pid, review_status="confirmed", need_review=0)
            flash(f"政策 #{pid} 已确认采纳", "ok")
        elif action == "reject":
            d.update_policy(pid, review_status="rejected", need_review=0)
            flash(f"政策 #{pid} 已剔除（不入库展示，记录保留）", "ok")
        elif action == "adjust":
            codes = [c for c in request.form.getlist("categories") if c in CAT_CODES]
            if not codes:
                flash("调整分类需至少勾选一个类别", "warn")
                return redirect(url_for("policy_detail", pid=pid))
            names = ",".join(_cat_label(c) for c in codes)
            d.update_policy(pid, category=",".join(codes), category_names=names,
                            review_status="adjusted", need_review=0)
            flash(f"政策 #{pid} 已调整为：{names}", "ok")
        else:
            flash("未知审核动作", "warn")
        return redirect(url_for("policy_detail", pid=pid))

    # ---------------- 来源与运行 ----------------
    @app.route("/sources")
    def sources():
        d = db()
        rows = d.list_sources()
        for s in rows:
            s["in_config"] = s["name"] in cfg.sources
        return render_template("sources.html", rows=rows)

    def _run_worker(source_name: str, prefer: str) -> None:
        with _RUN_LOCK:
            try:
                pipe = Pipeline(cfg)
                stats = pipe.run_source(source_name, prefer=prefer, limit=200)
                print(f"[web] run {source_name} done: {stats.to_dict()}")
            except Exception as e:  # noqa: BLE001
                print(f"[web] run {source_name} failed: {e}")

    @app.route("/sources/<name>/run", methods=["POST"])
    def source_run(name: str):
        src = cfg.sources.get(name)
        if src is None:
            flash(f"来源 {name} 不在当前 sources.yaml 中", "warn")
            return redirect(url_for("sources"))
        t = threading.Thread(target=_run_worker, args=(name, "rule"), daemon=True)
        t.start()
        flash(f"已开始运行来源 [{name}]（规则分类，无需 API Key），可在运行日志查看进度", "ok")
        return redirect(url_for("runs"))

    # ---------------- 运行日志 ----------------
    @app.route("/runs")
    def runs():
        d = db()
        rows = d.list_runs(limit=50)
        smap = {s["id"]: s["name"] for s in d.list_sources()}
        parsed = []
        for r in rows:
            try:
                r["summary_obj"] = json.loads(r["summary"] or "{}")
            except Exception:  # noqa: BLE001
                r["summary_obj"] = {}
            parsed.append(r)
        running = any(r["status"] == "running" for r in rows)
        return render_template("runs.html", rows=parsed, running=running, smap=smap)

    @app.get("/health")
    def health():
        return {"ok": True, "db": str(cfg.db_path)}

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="政策文件归集系统 Web 界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    app = create_app()
    if args.open:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")).start()
    print(f"政策文件归集系统 Web 界面: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
