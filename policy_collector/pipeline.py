"""Discover, fetch, parse attachments, classify, version and store with auditability."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from .classifier import Classifier
from .collector import Collector,ListPageParser,list_page_url
from .config import AppConfig,SourceConfig,PROJECT_ROOT
from .db import Database
from .dedup import Deduplicator,content_hash,make_policy_key
from .locking import ingestion_lock
from .models import Classification,Document,now
from .parser import Parser

@dataclass
class RunStats:
    discovered:int=0
    downloaded:int=0
    parsed:int=0
    ingested:int=0
    updated:int=0
    duplicates:int=0
    failed:int=0
    need_review:int=0
    excluded:int=0
    attachments_downloaded:int=0
    attachments_failed:int=0
    attachments_unparsed:int=0
    llm_classified:int=0
    rule_classified:int=0
    llm_fallback:int=0
    reclassified:int=0
    input_tokens:int=0
    output_tokens:int=0
    elapsed_seconds:float=0

    def to_dict(self):return asdict(self)
    def __add__(self,other):return RunStats(**{k:getattr(self,k)+getattr(other,k) for k in asdict(self)})
    @property
    def has_errors(self):return bool(self.failed or self.attachments_failed or self.llm_fallback)

class Pipeline:
    def __init__(self,cfg:AppConfig):
        self.cfg=cfg
        self.db=Database(cfg.db_path)
        self.collector=Collector(cfg)
        self.parser=Parser()
        self.classifier=Classifier(cfg)
        self.dedup=Deduplicator(self.db)
        self.discovery_errors=[]

    def close(self):
        self.collector.close();self.db.close()

    def _fmt(self,url,raw=b'',content_type=''):
        if raw.startswith(b'%PDF-'):return 'pdf'
        suffix=Path(urllib.parse.urlsplit(url).path).suffix.lower().lstrip('.')
        if suffix:return suffix
        if 'pdf' in content_type:return 'pdf'
        if 'wordprocessingml' in content_type:return 'docx'
        return 'html'

    def sync_sources(self):
        for name,src in self.cfg.sources.items():
            self.db.upsert_source(name=name,site=src.site,region=src.region,category=src.category,
                enabled=int(src.enabled),list_url=src.list_url,link_selector=src.link_selector,
                max_pages=src.max_pages,include=json.dumps(src.include),exclude=json.dumps(src.exclude))
        return len(self.cfg.sources)

    def _local(self,url):
        value=urllib.parse.unquote(url.removeprefix('file://'))
        p=Path(value)
        return p if p.is_absolute() else PROJECT_ROOT/p

    def discover(self,source,run_id=''):
        stats=RunStats();self.discovery_errors=[]
        row=self.db.get_source(source.name)
        links=[];seen_pages=set()
        for page in range(1,source.max_pages+1):
            url=source.feed_url if source.list_format=='gov_json' else list_page_url(source,page)
            if url in seen_pages:break
            seen_pages.add(url)
            try:
                if url.startswith('file://'):
                    p=self._local(url).resolve();raw=p.read_bytes();base=p.as_uri()
                else:
                    result=self.collector.fetch(url)
                    if not result.ok:raise ValueError(result.error)
                    raw=result.content;base=result.final_url or url
                # Preserve list originals as evidence, including failed/empty selector investigations.
                self.collector.save(source.name,url,raw,'html')
                from .site_adapters import get_list_parser
                custom=get_list_parser(source.name)
                if source.list_format == 'gov_json':
                    from .collector import parse_gov_feed
                    found = parse_gov_feed(raw,source)
                elif source.list_format == 'zj_unit':
                    from .collector import discover_zj_unit_links
                    found = discover_zj_unit_links(self.collector, source, raw, url,
                                                   max_pages=source.max_pages)
                elif custom:
                    from bs4 import BeautifulSoup
                    found=custom(BeautifulSoup(raw,'lxml'),source)
                else:
                    found=ListPageParser(source).parse(raw,base_url=base)
                if not found:
                    raise ValueError('未发现候选链接：请核实栏目、动态列表接口或选择器')
                links.extend(found)
            except Exception as e:
                stats.failed+=1
                self.discovery_errors.append(f'{url}: {e}')
        for item in links:
            if not self.collector.is_policy_url(item.url,source):continue
            if not self.db.get_fetch(row['id'],item.url):
                self.db.add_fetch(row['id'],item.url,title=item.title)
                stats.discovered+=1
        with self.db.tx() as c:
            c.execute('UPDATE source_configs SET last_checked_at=?,last_error=? WHERE id=?',
                      (now(),'\n'.join(self.discovery_errors),row['id']))
            if not self.discovery_errors:
                c.execute('UPDATE source_configs SET last_success_at=? WHERE id=?',(now(),row['id']))
        return stats

    def _attachments(self,source,doc,stats):
        for att in doc.attachments:
            att.update(local_path='',sha256='',parsed_text='',parse_status='failed',error='')
            try:
                if att['url'].startswith('file://'):
                    if not doc.page_url.startswith('file://'):raise ValueError('远程网页不能引用本地文件')
                    raw=self._local(att['url']).read_bytes()
                    content_type=''
                else:
                    result=self.collector.fetch(att['url'])
                    if not result.ok:raise ValueError(result.error)
                    raw=result.content;content_type=result.content_type
                fmt=self._fmt(att['url'],raw,content_type)
                if fmt not in ('pdf','docx','doc','txt','xls','xlsx','zip','rar','ofd','wps'):
                    fmt=att.get('fmt') or fmt
                # A HTTP-200 error page is not a successfully downloaded PDF.
                if fmt=='pdf' and not raw.startswith(b'%PDF-'):raise ValueError('PDF附件返回非PDF内容')
                if fmt in ('doc','docx') and not (raw.startswith(b'PK') or raw.startswith(b'\xd0\xcf\x11\xe0')):
                    raise ValueError('Word附件返回非Word内容')
                att['fmt']=fmt
                att['local_path']=self.collector.save(source.name,att['url'],raw,fmt)
                att['sha256']=hashlib.sha256(raw).hexdigest()
                stats.attachments_downloaded+=1
                parsed=self.parser.parse(raw,fmt,page_url=att['url'],origin_path=att['local_path'])
                att['parsed_text']=parsed.content
                att['error']=parsed.parse_error
                att['parse_status']='partial' if parsed.content and parsed.parse_error else (
                    'ok' if parsed.content else 'unsupported' if fmt not in ('pdf','docx','txt') else 'needs_ocr')
                if att['parse_status']!='ok':stats.attachments_unparsed+=1
            except Exception as e:
                att['error']=str(e)[:500]
                stats.attachments_failed+=1

    def ingest_url(self,source,url,raw=None,prefer='llm',reclassify=False):
        from .collector import allowed_url
        if not allowed_url(url,source):
            raise ValueError('网址不属于该来源允许的详情范围')
        with ingestion_lock(self.cfg.db_path):
            self.sync_sources()
            return self._ingest_url(source,url,raw,prefer,reclassify)

    def _ingest_url(self,source,url,raw=None,prefer='llm',reclassify=False):
        stats=RunStats()
        row=self.db.get_source(source.name)
        if not row:
            self.db.upsert_source(name=source.name,site=source.site,region=source.region,list_url=source.list_url)
            row=self.db.get_source(source.name)
        sid=row['id']
        existing=self.db.get_fetch(sid,url)
        fid=existing['id'] if existing else self.db.add_fetch(sid,url)
        self.db.update_fetch(fid,last_checked_at=now(),error='')
        try:
            base=url;ctype=''
            if raw is None:
                if url.startswith('file://'):raw=self._local(url).read_bytes()
                else:
                    result=self.collector.fetch(url)
                    if not result.ok:raise ValueError(result.error)
                    raw=result.content;base=result.final_url or url;ctype=result.content_type
            fmt=self._fmt(url,raw,ctype)
            origin=self.collector.save(source.name,url,raw,fmt)
            self.db.update_fetch(fid,status='downloaded',downloaded_at=now(),raw_path=origin,
                                 content_sha256=hashlib.sha256(raw).hexdigest())
            stats.downloaded+=1
            doc=self.parser.parse(raw,fmt,page_url=base,origin_path=origin)
            doc.page_url=url;doc.source_name=source.name
            if not doc.content.strip() and not doc.attachments:
                raise ValueError(doc.parse_error or '正文解析为空')
            if not doc.title:doc.title=(existing or {}).get('title','') or '待核实标题'
            self._attachments(source,doc,stats)
            stats.parsed+=1
            old=self.db.policy_for_url(url)
            if old and stats.attachments_failed:
                # Do not create a false revision simply because a previously available attachment failed today.
                raise ValueError('附件下载失败，保留已有政策版本并等待补采')
            decision=self.dedup.check(doc)
            if decision.decision=='duplicate_skip':
                self.db.link_source(decision.policy_id,fid,sid,url)
                if reclassify and not doc.parse_error and all(a['parse_status']=='ok' for a in doc.attachments) and self.db.get_policy(decision.policy_id)['review_status'] not in ('confirmed','adjusted','rejected'):
                    cls=self.classifier.classify(doc,prefer)
                    self._count_classification(cls,stats)
                    fields=self._policy_row(source,doc,cls,fid)
                    keys=('category','category_names','is_investment_policy','need_review','reason','evidence',
                          'model_version','review_status','reviewer_hint','classification_method','fallback_reason',
                          'input_truncated','input_tokens','output_tokens','doc_type')
                    self.db.update_policy(decision.policy_id,**{k:fields[k] for k in keys})
                    stats.reclassified+=1
                else:stats.duplicates+=1
                status='failed' if stats.attachments_failed else 'processed'
                self.db.update_fetch(fid,status=status,processed_at=now(),error='附件需补采' if stats.attachments_failed else '')
                return stats
            cls=self.classifier.classify(doc,prefer)
            if doc.parse_error or stats.attachments_failed or any(a['parse_status'] != 'ok' for a in doc.attachments):
                cls.need_review=True
                cls.reviewer_hint+='；原文或附件不完整，需补采/解析复核'
                if cls.is_investment_policy=='no':cls.is_investment_policy='pending'
            if doc.attachments and not any(a.get('parsed_text') for a in doc.attachments):
                cls.need_review=True;cls.reviewer_hint+='；附件正文尚未解析'
                if cls.is_investment_policy=='no':cls.is_investment_policy='pending'
            self._count_classification(cls,stats)
            self.db.update_fetch(fid,classification_json=json.dumps(asdict(cls),ensure_ascii=False))
            if cls.is_investment_policy=='no':
                stats.excluded+=1
                self.db.update_fetch(fid,status='excluded',processed_at=now(),error=cls.reason)
                return stats
            key=old['policy_key'] if old else make_policy_key(doc).key
            related=''
            if decision.decision=='suspected_duplicate':
                related=key
                key+=':variant:'+hashlib.sha256(url.encode()).hexdigest()[:16]
                cls.need_review=True;cls.reviewer_hint+='；同文号的其他来源内容不同，请核对转载或修订关系'
            fields=self._policy_row(source,doc,cls,fid)
            fields['related_policy_key']=related
            if old:
                # New evidence/version requires review; keep the old human decision on its historical version.
                fields.update(need_review=1,review_status='pending')
            pid,_=self.db.store_document(key,fields,doc.attachments,fid,sid,url)
            if old:stats.updated+=1
            else:stats.ingested+=1
            if fields['need_review']:stats.need_review+=1
            self.db.update_fetch(fid,status='failed' if stats.attachments_failed else 'processed',processed_at=now(),
                                 error='附件下载失败，等待补采' if stats.attachments_failed else '')
        except Exception as e:
            stats.failed+=1
            self.db.update_fetch(fid,status='failed',error=str(e)[:1000])
        return stats

    def _count_classification(self,cls,stats):
        if cls.method=='llm':stats.llm_classified+=1
        elif cls.method=='rule_fallback':stats.llm_fallback+=1
        else:stats.rule_classified+=1
        stats.input_tokens+=cls.input_tokens;stats.output_tokens+=cls.output_tokens

    def _policy_row(self,source,doc,cls,fid):
        return dict(title=doc.title,wenhao=doc.wenhao,issuing_authority=doc.issuing_authority,
            page_date=doc.page_date,doc_date=doc.doc_date,region=source.region,site=source.site,page_url=doc.page_url,
            doc_type=cls.doc_type,category=cls.category,category_names=cls.category_names,
            is_investment_policy=cls.is_investment_policy,need_review=int(cls.need_review),reason=cls.reason,
            evidence=cls.evidence,confidence=cls.confidence,model_version=cls.model_version,
            review_status='pending' if cls.need_review else ('rejected' if cls.is_investment_policy=='no' else 'confirmed_auto'),
            content=doc.content,content_sha256=content_hash(doc),source_fetch_id=fid,reviewer_hint=cls.reviewer_hint,
            classification_method=cls.method,fallback_reason=cls.fallback_reason,input_truncated=int(cls.input_truncated),
            input_tokens=cls.input_tokens,output_tokens=cls.output_tokens)

    def run_source(self,source_name,prefer='llm',limit=50,kind='manual',retry_only=False,reclassify=False):
        if limit < 1:raise ValueError('limit必须大于0')
        src=self.cfg.sources.get(source_name)
        if src is None:raise ValueError(f'未知来源: {source_name}')
        if not src.enabled:raise ValueError(f'来源未启用: {source_name}；请先核实配置与接入条件')
        with ingestion_lock(self.cfg.db_path):
            self.sync_sources()
            row=self.db.get_source(source_name)
            run_id=f'run-{uuid.uuid4().hex[:16]}'
            # The process lock proves any previously running ingest process has ended.
            with self.db.tx() as c:
                c.execute("UPDATE run_logs SET status='failed',finished_at=?,note=note || '；上次运行中断，待重试' WHERE status='running'",(now(),))
            self.db.start_run(run_id,row['id'],kind=kind)
            total=RunStats();start=time.monotonic();note=''
            try:
                if not retry_only:
                    total+=self.discover(src,run_id)
                    note='\n'.join(self.discovery_errors)
                # Fairly rotate all known URLs, including processed/old pages. New discoveries and
                # failures get priority, but their oldest check time prevents retry starvation.
                where=" AND status='failed'" if retry_only else ''
                queue=self.db._conn.execute(f"""SELECT * FROM fetch_records WHERE source_id=? {where}
                    ORDER BY CASE WHEN status IN ('discovered','failed','downloaded') THEN 0 ELSE 1 END,
                    COALESCE(last_checked_at,''),id LIMIT ?""",(row['id'],limit)).fetchall()
                for fr in queue:
                    total+=self._ingest_url(src,fr['page_url'],prefer=prefer,reclassify=reclassify)
                    with self.db.tx() as c:
                        c.execute('UPDATE run_logs SET summary=? WHERE run_id=?',(json.dumps(total.to_dict()),run_id))
                status='partial' if total.has_errors else 'ok'
                if total.failed and not total.downloaded:status='failed'
            except Exception as e:
                total.failed+=1;status='failed';note+='\n'+str(e)
            total.elapsed_seconds=round(time.monotonic()-start,3)
            self.db.finish_run(run_id,total.to_dict(),status=status,model_version=self.cfg.llm.effective_model if total.llm_classified else '',note=note)
            return total

    def run_demo(self,samples_dir:Optional[Path]=None):
        src=SourceConfig(name='demo_local',site='本地样例（合成数据）',region='样例',category='演示',list_url='file://samples/list.html')
        with ingestion_lock(self.cfg.db_path):
            self.db.upsert_source(name=src.name,site=src.site,region=src.region,enabled=0,list_url=src.list_url)
            row=self.db.get_source(src.name);run_id=f'demo-{uuid.uuid4().hex[:16]}'
            self.db.start_run(run_id,row['id'],kind='demo')
            total=RunStats()
            for f in sorted((samples_dir or PROJECT_ROOT/'samples'/'policies').glob('*.html')):
                total.discovered+=1
                total+=self._ingest_url(src,f.resolve().as_uri(),prefer='rule')
            self.db.finish_run(run_id,total.to_dict(),status='partial' if total.has_errors else 'ok')
            return total
