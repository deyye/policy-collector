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
    documents_incomplete:int=0
    llm_classified:int=0
    rule_classified:int=0
    llm_fallback:int=0
    reclassified:int=0
    reparsed:int=0
    attachments_cached:int=0
    input_tokens:int=0
    output_tokens:int=0
    elapsed_seconds:float=0

    def to_dict(self):return asdict(self)
    def __add__(self,other):return RunStats(**{k:getattr(self,k)+getattr(other,k) for k in asdict(self)})
    @property
    def has_errors(self):return bool(self.failed or self.attachments_failed or self.attachments_unparsed or self.documents_incomplete or self.llm_fallback)

class Pipeline:
    def __init__(self,cfg:AppConfig):
        self.cfg=cfg
        self.db=Database(cfg.db_path)
        self.collector=Collector(cfg)
        self.parser=Parser()
        self.classifier=Classifier(cfg)
        self.dedup=Deduplicator(self.db)
        self.discovery_errors=[]
        self.active_run = ''
        self.active_fetch = None
        from .agent import PolicyAgent
        self.agent = PolicyAgent(self.classifier, self._event)

    def _event(self, action, status, message):
        self.db.agent_event(self.active_run, self.active_fetch, action, status, message)
        self.db.run_progress(self.active_run, stage=action, message=message)

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
        links=[];seen_pages=set();seen_links=set();next_url=''
        self.collector.discovery_status[source.name] = {
            'pages_fetched': 0, 'end_reached': False, 'stop_reason': 'page_limit'}
        for page in range(1,source.max_pages+1):
            url=source.feed_url if source.list_format=='gov_json' else (next_url or list_page_url(source,page))
            if url in seen_pages:
                self.collector.discovery_status[source.name]['stop_reason']='single_window'
                break
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
                elif source.list_format == 'jpage':
                    from .collector import discover_jpage_links
                    found = discover_jpage_links(self.collector, source, raw, base, source.max_pages)
                elif custom:
                    from bs4 import BeautifulSoup
                    found=custom(BeautifulSoup(raw,'lxml'),source)
                else:
                    found=ListPageParser(source).parse(raw,base_url=base)
                if not found:
                    raise ValueError('未发现候选链接：请核实栏目、动态列表接口或选择器')
                fresh = [item for item in found if item.url not in seen_links]
                if not fresh:
                    raise ValueError('列表页完全重复，未确认历史采完')
                seen_links.update(item.url for item in fresh)
                links.extend(fresh)
                if source.list_format in ('zj_unit', 'jpage'):
                    break
                status = self.collector.discovery_status[source.name]
                status['pages_fetched'] += 1
                if source.list_format == 'gov_json':
                    status['stop_reason'] = 'feed_window'
                    break
                if source.pagination == 'next_link':
                    from .collector import next_page_link
                    next_url = next_page_link(raw, base)
                    if not next_url:
                        status['stop_reason'] = 'no_next_link'
                        break
                elif source.pagination == 'single':
                    status['stop_reason'] = 'single_window'
                    break
            except Exception as e:
                links.extend(getattr(e, 'links', []))
                stats.failed+=1
                self.discovery_errors.append(f'{url}: {e}')
                self.collector.discovery_status[source.name]['stop_reason']='error'
                break
        for item in links:
            admitted=self.collector.is_policy_url(item.url,source)
            with self.db.tx() as c:
                c.execute("INSERT INTO discovery_observations(source_id,page_url,title,admitted,reason,observed_at) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,page_url) DO UPDATE SET title=excluded.title,admitted=excluded.admitted,reason=excluded.reason,observed_at=excluded.observed_at",
                    (row['id'],item.url,item.title,int(admitted),'' if admitted else '来源URL范围过滤',now()))
            if not admitted:continue
            if not self.db.get_fetch(row['id'],item.url):
                self.db.add_fetch(row['id'],item.url,title=item.title)
                stats.discovered+=1
        with self.db.tx() as c:
            c.execute('UPDATE source_configs SET last_checked_at=?,last_error=? WHERE id=?',
                      (now(),'\n'.join(self.discovery_errors),row['id']))
            if not self.discovery_errors:
                c.execute('UPDATE source_configs SET last_success_at=? WHERE id=?',(now(),row['id']))
        return stats

    def _attachments(self,source,doc,stats,reuse_cached=False,local_only=False,only_urls=None):
        old=self.db.policy_for_url(doc.page_url)
        cached={a['url']:a for a in self.db.list_attachments(old['id'])} if old else {}
        for att in doc.attachments:
            if only_urls is not None and att['url'] not in only_urls: continue
            att.update(local_path='',sha256='',parsed_text='',parse_status='failed',error='')
            try:
                cache=cached.get(att['url'],{})
                cached_path=Path(cache.get('local_path') or '/nonexistent-policy-cache')
                if reuse_cached and cached_path.is_file() and hashlib.sha256(cached_path.read_bytes()).hexdigest()==cache.get('sha256'):
                    raw=cached_path.read_bytes();content_type='';stats.attachments_cached+=1
                elif local_only:
                    raise ValueError('本地缺少校验通过的原件，需联网补采')
                elif att['url'].startswith('file://'):
                    if not doc.page_url.startswith('file://'):raise ValueError('远程网页不能引用本地文件')
                    raw=self._local(att['url']).read_bytes()
                    content_type=''
                else:
                    result=self.collector.fetch(att['url'])
                    if not result.ok:raise ValueError(result.error)
                    raw=result.content;content_type=result.content_type
                from .attachment_parsers import detect_format, PARSER_VERSION
                fmt=detect_format(raw,att.get('fmt') or self._fmt(att['url'],raw,content_type))
                if fmt not in ('pdf','docx','doc','txt','xls','xlsx','zip','rar','ofd','wps'):
                    fmt=att.get('fmt') or fmt
                if fmt != 'txt' and ('text/html' in content_type.lower() or raw.lstrip().lower().startswith((b'<!doctype html', b'<html'))):
                    raise ValueError('附件返回HTML网页，未取得文件原件')
                # A HTTP-200 error page is not a successfully downloaded PDF.
                if fmt=='pdf' and not raw.startswith(b'%PDF-'):raise ValueError('PDF附件返回非PDF内容')
                if fmt in ('doc','docx') and not (raw.startswith(b'PK') or raw.startswith(b'\xd0\xcf\x11\xe0')):
                    raise ValueError('Word附件返回非Word内容')
                att['fmt']=fmt
                att['local_path']=self.collector.save(source.name,att['url'],raw,fmt)
                att['sha256']=hashlib.sha256(raw).hexdigest()
                stats.attachments_downloaded+=1
                parsed=self.parser.parse(raw,fmt,page_url=att['url'],origin_path=att['local_path'])
                att.update(parser_version=PARSER_VERSION,parse_method=parsed.parse_method,total_pages=parsed.total_pages,parsed_pages=parsed.parsed_pages)
                att['parsed_text']=parsed.content
                att['error']=parsed.parse_error
                att['parse_status']='partial' if parsed.content and parsed.parse_error else (
                    'ok' if parsed.content else 'unsupported' if fmt not in ('pdf','docx','txt','ofd','doc','wps') else 'needs_ocr')
                if att['parse_status']!='ok':stats.attachments_unparsed+=1
            except Exception as e:
                att['error']=str(e)[:500]
                if att.get('sha256'):stats.attachments_unparsed+=1
                else:stats.attachments_failed+=1

    def ingest_url(self,source,url,raw=None,prefer='llm',reclassify=False):
        from .collector import allowed_url
        if not allowed_url(url,source):
            raise ValueError('网址不属于该来源允许的详情范围')
        with ingestion_lock(self.cfg.db_path):
            self.sync_sources()
            return self._ingest_url(source,url,raw,prefer,reclassify)

    def _ingest_url(self,source,url,raw=None,prefer='llm',reclassify=False,reuse_cached=False,local_only=False):
        stats=RunStats()
        row=self.db.get_source(source.name)
        if not row:
            self.db.upsert_source(name=source.name,site=source.site,region=source.region,list_url=source.list_url)
            row=self.db.get_source(source.name)
        sid=row['id']
        existing=self.db.get_fetch(sid,url)
        fid=existing['id'] if existing else self.db.add_fetch(sid,url)
        self.active_fetch = fid
        self._event('download', 'running', '正在获取原文')
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
            self._event('parse', 'running', '正在提取正文、元数据与附件')
            doc=self.parser.parse(raw,fmt,page_url=base,origin_path=origin)
            doc.page_url=url;doc.source_name=source.name;doc.raw_bytes_sha256=hashlib.sha256(raw).hexdigest()
            if not doc.content.strip() and not doc.attachments:
                raise ValueError(doc.parse_error or '正文解析为空')
            if not doc.title:doc.title=(existing or {}).get('title','') or '待核实标题'
            self._attachments(source,doc,stats,reuse_cached,local_only)
            self.db.record_attachment_attempts(fid,doc.attachments)
            def repair(urls):
                retry_stats=RunStats()
                self._attachments(source,doc,retry_stats,only_urls=urls)
                stats.attachments_downloaded += retry_stats.attachments_downloaded
                stats.attachments_failed = sum(a.get('parse_status')!='ok' and not a.get('sha256') for a in doc.attachments)
                stats.attachments_unparsed = sum(a.get('parse_status')!='ok' and bool(a.get('sha256')) for a in doc.attachments)
                self.db.record_attachment_attempts(fid,[a for a in doc.attachments if a['url'] in urls])
            if not local_only:
                self.agent.classifier=self.classifier
                self.agent.prepare(doc,repair,prefer)
                stats.input_tokens += self.agent.usage.get('input_tokens',0)
                stats.output_tokens += self.agent.usage.get('output_tokens',0)
            self.db.update_fetch(fid,document_json=json.dumps(asdict(doc),ensure_ascii=False))
            stats.parsed+=1
            if doc.parse_error:
                stats.documents_incomplete+=1
            old=self.db.policy_for_url(url)
            self._event('dedup', 'running', '正在核对重复文件与已有版本')
            if old:
                reclassify = reclassify or self._needs_model_retry(old, prefer)
                previous=self.db.list_attachments(old['id'])
                by_url={a['url']:a for a in previous}
                raw_before=old.get('raw_page_sha256') or ((existing or {}).get('content_sha256') if ((existing or {}).get('policy_id')==old['id'] or (existing or {}).get('id')==old.get('source_fetch_id')) else '')
                same_original=((raw_before==doc.raw_bytes_sha256 or old['content_sha256']==content_hash(doc)) and set(by_url)=={a['url'] for a in doc.attachments}
                    and all(not by_url[a['url']].get('sha256') or not a.get('sha256') or by_url[a['url']]['sha256']==a['sha256'] for a in doc.attachments))
                if same_original:
                    # Keep previous good extraction if this attempt is poorer; attempts retain the new error.
                    merged=[]
                    for a in doc.attachments:
                        prev=by_url[a['url']]
                        if not a.get('sha256') or (prev.get('parse_status')=='ok' and a.get('parse_status')!='ok'):
                            merged.append(prev)
                        else: merged.append(a)
                    if doc.parse_error and old['content'] and not old.get('parse_error'):
                        doc.content=old['content'];doc.parse_error=old.get('parse_error','')
                    doc.attachments=merged
                    changed=(doc.content!=old['content'] or doc.parse_error!=old.get('parse_error','') or
                        any(any(a.get(k,'')!=by_url[a['url']].get(k,'') for k in ('sha256','parsed_text','parse_status')) for a in merged))
                    fields=dict(content=doc.content,content_sha256=content_hash(doc),
                        analysis_sha256=hashlib.sha256(doc.analysis_text.encode()).hexdigest(),
                        raw_page_sha256=doc.raw_bytes_sha256,parse_error=doc.parse_error)
                    # Metadata backfill remains supported on same-source reparses.
                    for key in ('wenhao','page_date','doc_date','issuing_authority'):
                        if not old.get(key) and getattr(doc,key):fields[key]=getattr(doc,key)
                    if (changed or reclassify) and not stats.attachments_failed and old['review_status'] not in ('confirmed','adjusted','rejected'):
                        cls=self._classify_document(doc,prefer);self._count_classification(cls,stats)
                        rowfields=self._policy_row(source,doc,cls,fid)
                        fields.update({k:v for k,v in rowfields.items() if k not in ('title','wenhao','page_date','doc_date','issuing_authority','region','site','page_url','source_fetch_id')})
                        stats.reclassified+=1
                        self.db.update_fetch(fid,classification_json=json.dumps(asdict(cls),ensure_ascii=False))
                    self.db.refresh_analysis(old['id'],fields,merged,changed)
                    if changed:stats.reparsed+=1
                    elif not stats.reclassified:stats.duplicates+=1
                    self.db.update_fetch(fid,status='failed' if stats.attachments_failed else 'processed',processed_at=now(),error='附件需补采' if stats.attachments_failed else '')
                    stats.failed+=int(bool(stats.attachments_failed))
                    return stats
            if old and stats.attachments_failed:
                # Do not create a false revision simply because a previously available attachment failed today.
                raise ValueError('附件下载失败，保留已有政策版本并等待补采')
            decision=self.dedup.check(doc)
            if decision.decision=='duplicate_skip':
                self.db.link_source(decision.policy_id,fid,sid,url)
                if old and old['id'] == decision.policy_id:
                    # 同一来源重采可补齐空元数据，不改业务分类或伪造正文修订版本。
                    current = self.db.get_policy(decision.policy_id)
                    filled = {k: getattr(doc, k) for k in ('wenhao','page_date','doc_date','issuing_authority')
                              if not current.get(k) and getattr(doc, k)}
                    if filled:
                        with self.db.tx() as c:
                            c.execute('UPDATE policies SET '+','.join(k+'=?' for k in filled)+' WHERE id=?',
                                      (*filled.values(), decision.policy_id))
                            c.execute('INSERT INTO review_events(policy_id,action,before_json,after_json,note,created_at) VALUES(?,?,?,?,?,?)',
                                (decision.policy_id, 'metadata_backfill',
                                 json.dumps({k:current.get(k) for k in filled},ensure_ascii=False),
                                 json.dumps(filled,ensure_ascii=False), '同一来源重采补齐空字段：'+url, now()))
                reclassify = reclassify or self._needs_model_retry(self.db.get_policy(decision.policy_id), prefer)
                if reclassify and not doc.parse_error and all(a['parse_status']=='ok' for a in doc.attachments) and self.db.get_policy(decision.policy_id)['review_status'] not in ('confirmed','adjusted','rejected'):
                    cls=self._classify_document(doc,prefer)
                    self._count_classification(cls,stats)
                    fields=self._policy_row(source,doc,cls,fid)
                    keys=('category','category_names','is_investment_policy','need_review','reason','evidence',
                          'model_version','review_status','reviewer_hint','classification_method','fallback_reason',
                          'input_truncated','input_tokens','output_tokens','doc_type','parse_requires_review')
                    self.db.update_policy(decision.policy_id,**{k:fields[k] for k in keys})
                    stats.reclassified+=1
                else:stats.duplicates+=1
                status='failed' if stats.attachments_failed else 'processed'
                self.db.update_fetch(fid,status=status,processed_at=now(),error='附件需补采' if stats.attachments_failed else '')
                return stats
            cls=self._classify_document(doc,prefer)
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
                self._event('excluded','done','明确不属于归集范围，保留采集记录')
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
            self._event('store','running','正在保存政策、附件及来源')
            pid,_=self.db.store_document(key,fields,doc.attachments,fid,sid,url)
            if old:stats.updated+=1
            else:stats.ingested+=1
            if fields['need_review']:stats.need_review+=1
            self.db.update_fetch(fid,status='failed' if stats.attachments_failed else 'processed',processed_at=now(),
                                 error='附件下载失败，等待补采' if stats.attachments_failed else '')
        except Exception as e:
            self._event('failed','attention','处理未完成：'+str(e)[:240])
            stats.failed+=1
            self.db.update_fetch(fid,status='failed',error=str(e)[:1000])
        return stats

    def _needs_model_retry(self, row, prefer):
        # Normal scheduled rotation supplies the retry budget; no tight retry loop.
        return (prefer == 'llm' and self.classifier.llm.available
                and row.get('classification_method') == 'rule_fallback'
                and row.get('review_status') not in ('confirmed', 'adjusted', 'rejected'))

    def _classify_document(self, doc, prefer):
        self.agent.classifier=self.classifier
        cls = self.agent.classify(doc, prefer)
        # A tentative negative belongs in the review queue, not the exclusion log.
        if cls.is_investment_policy == 'no' and cls.need_review:
            cls.is_investment_policy = 'pending'
        return cls

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
            raw_page_sha256=doc.raw_bytes_sha256,analysis_sha256=hashlib.sha256(doc.analysis_text.encode()).hexdigest(),parse_error=doc.parse_error,
            parse_requires_review=int(bool(doc.parse_error or any(a.get('parse_status') != 'ok' for a in doc.attachments))),
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
            self.active_run=run_id
            self.active_fetch=None
            self._event('discover','running','正在发现官网政策文件；此阶段暂不估计总量')
            total=RunStats();start=time.monotonic();note=''
            try:
                if not retry_only:
                    total+=self.discover(src,run_id)
                    note='\n'.join(self.discovery_errors)
                    discovery=self.collector.discovery_status.get(src.name,{})
                    note += '\n采集范围：' + json.dumps(discovery,ensure_ascii=False)
                    if not discovery.get('end_reached'):
                        note += '；本次未确认历史列表全部采完'
                # 新发现优先，其余按最近检查时间轮转；持续失败不能永远压住已采记录。
                where=" AND status='failed'" if retry_only else ''
                queue=self.db._conn.execute(f"""SELECT * FROM fetch_records WHERE source_id=? {where}
                    ORDER BY COALESCE(last_checked_at,''),id LIMIT ?""",(row['id'],limit)).fetchall()
                self.db.run_progress(run_id, total=len(queue), completed=0)
                for index,fr in enumerate(queue):
                    self.db.run_progress(run_id, title=fr['title'] or fr['page_url'])
                    total+=self._ingest_url(src,fr['page_url'],prefer=prefer,reclassify=reclassify)
                    self._event('document_done','done','本份文件已处理，结果见记录')
                    self.db.run_progress(run_id, completed=index+1)
                    with self.db.tx() as c:
                        c.execute('UPDATE run_logs SET summary=? WHERE run_id=?',(json.dumps(total.to_dict()),run_id))
                status='partial' if total.has_errors else 'ok'
                if total.failed and not total.downloaded:status='failed'
            except Exception as e:
                total.failed+=1;status='failed';note+='\n'+str(e)
            total.elapsed_seconds=round(time.monotonic()-start,3)
            self.db.run_progress(run_id, stage='finished', message='本批处理结束；有待办请继续处理')
            self.db.finish_run(run_id,total.to_dict(),status=status,model_version=self.cfg.llm.effective_model if total.llm_classified else '',note=note)
            self.active_run=''
            self.active_fetch=None
            return total

    def run_demo(self,samples_dir:Optional[Path]=None):
        src=SourceConfig(name='demo_local',site='本地样例（合成数据）',region='样例',category='演示',list_url='file://samples/list.html')
        with ingestion_lock(self.cfg.db_path):
            self.db.upsert_source(name=src.name,site=src.site,region=src.region,enabled=0,list_url=src.list_url)
            row=self.db.get_source(src.name);run_id=f'demo-{uuid.uuid4().hex[:16]}'
            self.db.start_run(run_id,row['id'],kind='demo')
            self.active_run=run_id
            files=sorted((samples_dir or PROJECT_ROOT/'samples'/'policies').glob('*.html'))
            self.db.run_progress(run_id,total=len(files),completed=0,title='离线样例')
            total=RunStats()
            for index,f in enumerate(files):
                total.discovered+=1
                total+=self._ingest_url(src,f.resolve().as_uri(),prefer='rule')
                self.db.run_progress(run_id,completed=index+1)
            self.db.run_progress(run_id,stage='finished',message='离线样例处理完成')
            self.db.finish_run(run_id,total.to_dict(),status='partial' if total.has_errors else 'ok')
            self.active_run=''
            self.active_fetch=None
            return total
