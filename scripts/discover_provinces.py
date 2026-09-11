"""Discover policy columns from the official provincial directory; never auto-enable them.
python scripts/discover_provinces.py --output docs/province-discovery.json
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin,urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from policy_collector.collector import Collector
from policy_collector.config import AppConfig


def probe(entry):
    cfg=AppConfig();cfg.fetch.timeout_seconds=10;cfg.fetch.retries=0
    collector=Collector(cfg)
    try:
        # Prefer HTTPS, retaining the official directory's original URL as evidence.
        home=entry['home_url'].replace('http:','https:',1)
        fetched=collector.fetch(home)
        if not fetched.ok:return {**entry,'status':'blocked','error':fetched.error}
        final=fetched.final_url or home
        soup=BeautifulSoup(fetched.content,'lxml');columns=[];seen=set()
        for a in soup.find_all('a',href=True):
            title=a.get_text(' ',strip=True);url=urljoin(final,a['href'])
            if len(title)>24 or not re.fullmatch(r'(?:行政)?规范性文件(?:库)?|政策文件|政策法规|通知公告|政策发布|法规文件|其他文件|吉林政策',title):continue
            if urlsplit(url).hostname!=urlsplit(final).hostname or urlsplit(url).scheme not in ('http','https') or url in seen:continue
            seen.add(url);columns.append({'title':title,'url':url})
        return {**entry,'status':'columns_found' if columns else 'needs_adapter','final_url':final,
                'home_sha256':fetched.sha256,'columns':columns}
    finally:collector.close()


def main():
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument('--registry',default='config/provinces.json')
    args.add_argument('--output',default='docs/province-discovery.json')
    ns=args.parse_args();entries=json.loads(Path(ns.registry).read_text())
    def run(entry):
        result=probe(entry);print(entry['region'],result['status'],flush=True);return result
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(run,entries))
    path=Path(ns.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),
        'note':'官网首页与栏目候选探测；未接入不自动启用，仍须运行audit_sources验证列表、正文、附件和入库。',
        'regions':results},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
