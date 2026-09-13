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
import secrets
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for, g, session, send_file

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
    app.secret_key = secrets.token_hex(32)
    app.cfg = cfg

    def db() -> Database:
        if 'database' not in g:
            g.database = Database(cfg.db_path)
        return g.database

    @app.teardown_appcontext
    def close_db(error=None):
        d = g.pop('database', None)
        if d is not None: d.close()

    @app.before_request
    def protect_forms():
        session.setdefault('csrf', secrets.token_hex(24))
        if request.method == 'POST' and not secrets.compare_digest(request.form.get('csrf_token', ''), session['csrf']):
            abort(400, '表单已失效，请刷新页面重试')

    @app.context_processor
    def form_token():
        return {'csrf_token': session.get('csrf','')}

    boot = Pipeline(cfg)
    try:
        boot.sync_sources()
    finally:
        boot.close()

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
        versions = d.versions(p["policy_key"])
        attachments = d.list_attachments(pid)
        from .quality import attachment_quality
        for a in attachments:a.update(attachment_quality(a))
        material_issues=[dict(r) for r in d._conn.execute("SELECT t.* FROM attachment_attempts t WHERE t.fetch_id IN (SELECT fetch_id FROM policy_sources WHERE policy_id=?) AND t.id=(SELECT MAX(t2.id) FROM attachment_attempts t2 WHERE t2.fetch_id=t.fetch_id AND t2.url=t.url) AND (t.download_status!='ok' OR t.parse_status!='ok')",(pid,))]
        cats = _policy_category_names(p)
        return render_template(
            "policy.html", p=p, versions=versions, attachments=attachments, cats=cats, material_issues=material_issues,
            provenance=d.policy_sources(p["policy_key"]), history=d.review_history(pid), agent_events=d.policy_agent_events(pid),
            cat_codes=CAT_CODES, _cat_label=_cat_label, review_style=_REVIEW_STYLE.get(p["review_status"], ""),
        )

    @app.get('/quality')
    def material_quality():
        from .quality import attachment_report
        return render_template('quality.html',report=attachment_report(db()))

    # ---------------- 人工复核 ----------------
    @app.route("/policies/<int:pid>/review", methods=["POST"])
    def policy_review(pid: int):
        d = db()
        p = d.get_policy(pid)
        if p is None:
            abort(404)
        action = request.form.get("action", "")
        try:
            d.audit(pid, action, request.form.getlist('categories'), request.form.get('note',''))
            flash(f"政策 #{pid} 已完成复核，变更已留痕", 'ok')
        except ValueError as exc:
            flash(str(exc), 'warn')
        return redirect(url_for("policy_detail", pid=pid))

    # ---------------- 来源与运行 ----------------
    @app.route("/sources")
    def sources():
        d = db()
        rows = d.list_sources()
        for s in rows:
            s["in_config"] = s["name"] in cfg.sources
            s["note"] = cfg.sources[s["name"]].note if s["in_config"] else ""
        from .llm_client import LLMClient
        return render_template("sources.html", rows=rows, model_ready=LLMClient(cfg.llm).available)

    def _run_worker(source_name: str, prefer: str, retry_only=False) -> None:
        pipe = None
        try:
            pipe = Pipeline(cfg)
            pipe.run_source(source_name, prefer=prefer, limit=200, retry_only=retry_only)
        finally:
            if pipe: pipe.close()
            _RUN_LOCK.release()

    @app.route("/sources/<name>/run", methods=["POST"])
    def source_run(name: str):
        src = cfg.sources.get(name)
        if src is None or not src.enabled:
            flash(f"来源 {name} 不在当前 sources.yaml 中", "warn")
            return redirect(url_for("sources"))
        prefer = request.form.get('prefer', 'llm')
        if prefer not in ('llm','rule'): abort(400)
        if not _RUN_LOCK.acquire(blocking=False):
            flash('已有采集任务运行，请等待完成', 'warn')
            return redirect(url_for('runs'))
        t = threading.Thread(target=_run_worker, args=(name, prefer, request.form.get('retry_only') == '1'), daemon=True)
        try: t.start()
        except Exception:
            _RUN_LOCK.release()
            raise
        flash("任务已启动，下方会自动显示处理进度。", "ok")
        return redirect(url_for("runs"))

    # ---------------- 运行日志 ----------------
    @app.route("/runs")
    def runs():
        d = db()
        rows = d.list_runs(limit=50)
        smap = {s["id"]: (s["site"] or s["name"]) for s in d.list_sources()}
        parsed = []
        for r in rows:
            try:
                r["summary_obj"] = json.loads(r["summary"] or "{}")
                r["progress_obj"] = json.loads(r.get("progress") or "{}")
            except Exception:  # noqa: BLE001
                r["summary_obj"] = {}
                r["progress_obj"] = {}
            parsed.append(r)
        running = _RUN_LOCK.locked() or any(r["status"] == "running" for r in rows)
        return render_template("runs.html", rows=parsed, running=running, smap=smap)

    @app.get('/runs/<run_id>')
    def run_detail(run_id):
        d=db()
        row=d._conn.execute('SELECT * FROM run_logs WHERE run_id=?',(run_id,)).fetchone()
        if row is None: abort(404)
        r=dict(row)
        r['summary_obj']=json.loads(r['summary'] or '{}')
        r['progress_obj']=json.loads(r.get('progress') or '{}')
        source=next((s for s in d.list_sources() if s['id']==r['source_id']),{})
        return render_template('run_detail.html',r=r,source=source,events=d.run_events(run_id),running=r['status']=='running')

    @app.get('/attachments/<int:aid>/download')
    def attachment_download(aid):
        a = db().get_attachment(aid)
        if not a or not a['local_path']: abort(404)
        path = Path(a['local_path']).resolve()
        if not path.is_relative_to(cfg.downloads_dir.resolve()) or not path.is_file(): abort(404)
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.route('/settings/model', methods=['GET','POST'])
    def model_settings():
        from .model_settings import save_model_settings, connection_check
        result=None
        if request.method=='POST':
            if _RUN_LOCK.locked():
                flash('采集运行中，请结束后再修改或检查模型配置','warn')
                return redirect(url_for('model_settings'))
            try:
                if request.form.get('action')=='check':
                    result=connection_check(cfg)
                else:
                    save_model_settings(cfg,request.form.get('provider','dashscope'),
                        request.form.get('base_url',''),request.form.get('model',''),
                        request.form.get('api_key','').strip(),request.form.get('api_key_env',''))
                    flash('配置已保存在本机，后续新采集任务使用新配置；可点击检查连接。','ok')
                    return redirect(url_for('model_settings'))
            except ValueError as exc:
                flash(str(exc),'warn')
        return render_template('model_settings.html',base_url=cfg.llm.base_url,model=cfg.llm.effective_model,
            key_env=cfg.llm.api_key_env,key_configured=bool(cfg.llm.api_key),result=result,
            provider='dashscope' if 'aliyun' in cfg.llm.base_url else 'custom')

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
