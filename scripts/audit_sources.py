"""Repeatable source acceptance: list discovery -> detail -> attachment -> DB.

python scripts/audit_sources.py --source yn_policies,jl_policies --pages 2 --details 2
Reports are evidence for the sampled window, never a claim of province-wide recall.
Uses an isolated temporary database, never edits an existing policy database.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_collector.config import AppConfig
from policy_collector.pipeline import Pipeline


def audit_source(name, pages, details):
    cfg = AppConfig.load()
    cfg.fetch.timeout_seconds = 12
    cfg.fetch.retries = 0
    src = replace(cfg.sources[name], max_pages=pages)
    with tempfile.TemporaryDirectory(prefix='policy-audit-') as folder:
        cfg.data_dir = Path(folder)
        cfg.downloads_dir = cfg.data_dir / 'originals'
        cfg.db_path = cfg.data_dir / 'audit.db'
        cfg.sources = {name: src}
        pipe = Pipeline(cfg)
        try:
            pipe.sync_sources()
            discovered = pipe.discover(src)
            source_row = pipe.db.get_source(name)
            rows = pipe.db._conn.execute('SELECT * FROM fetch_records WHERE source_id=? ORDER BY id LIMIT ?', (source_row['id'], details)).fetchall()
            samples = []
            for row in rows:
                stat = pipe.ingest_url(src, row['page_url'], prefer='rule')
                record = pipe.db.get_fetch(source_row['id'], row['page_url'])
                doc = json.loads(record.get('document_json') or '{}')
                samples.append({'url': row['page_url'], 'title': doc.get('title',row['title']),
                    'stats': stat.to_dict(), 'fetch_status': record['status'],
                    'metadata': {k: doc.get(k,'') for k in ('page_date','doc_date','wenhao')},
                    'body_chars': len(doc.get('content','')), 'parse_error':doc.get('parse_error',''),
                    'attachments': [{k:a.get(k,'') for k in ('name','url','parse_status','error')} for a in doc.get('attachments',[])],
                    'error':record.get('error','')})
            return {'source':name,'region':src.region,'column':src.category,'url':src.list_url,
                'discovered':discovered.discovered,'discovery':pipe.collector.discovery_status.get(name,{}),
                'errors':pipe.discovery_errors,'sample_count':len(samples),'samples':samples,
                'classification_validation':'规则模式仅验证流程，未验证真实大模型准确率',
                'accepted_sample':bool(samples) and not pipe.discovery_errors and all(
                    s['body_chars'] > 0 and not any(s['stats'][k] for k in
                    ('failed','documents_incomplete','attachments_failed','attachments_unparsed')) for s in samples)}
        finally:
            pipe.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='')
    parser.add_argument('--pages',type=int,default=2)
    parser.add_argument('--details',type=int,default=2)
    parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--output',default='docs/source-audit.json')
    args=parser.parse_args()
    if min(args.pages,args.details,args.workers)<1:parser.error('pages/details/workers must be positive')
    cfg=AppConfig.load()
    names=args.source.split(',') if args.source else [n for n,s in cfg.sources.items() if s.enabled]
    for name in names:
        if name not in cfg.sources:parser.error(f'unknown source: {name}')
    def run(name):
        try:out=audit_source(name,args.pages,args.details)
        except Exception as exc:out={'source':name,'accepted_sample':False,'error':f'{type(exc).__name__}: {exc}'}
        print(name, 'sample_pass' if out['accepted_sample'] else 'needs_attention',flush=True)
        return out
    with ThreadPoolExecutor(max_workers=min(args.workers,6)) as pool:results=list(pool.map(run,names))
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
        'pages_limit':args.pages,'details_limit':args.details,'scope':'指定栏目的抽样接入验收；非全省政策覆盖率，非业务分类准确率',
        'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if all(r['accepted_sample'] for r in results) else 1

if __name__=='__main__':raise SystemExit(main())
