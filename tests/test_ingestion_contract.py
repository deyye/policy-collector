import base64
import json
from pathlib import Path
from urllib.parse import quote_plus
import pytest
from policy_collector.classifier import RuleClassifier, LLMClassifier
from policy_collector.collector import (Collector, FetchResult, DiscoveryError,
    discover_jpage_links, next_page_link)
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.models import Document
from policy_collector.pipeline import Pipeline


def config(tmp_path):
    cfg=AppConfig.load();cfg.data_dir=tmp_path;cfg.downloads_dir=tmp_path/'raw';cfg.db_path=tmp_path/'db.sqlite'
    return cfg


def test_two_generic_keywords_are_not_a_policy(tmp_path):
    rule=RuleClassifier(config(tmp_path).classification)
    out=rule.classify(Document(title='关于报送工作总结的通知',content='今年投资增长较快，项目推进顺利。'))
    assert out.is_investment_policy == 'pending'
    assert not out.evidence


@pytest.mark.parametrize('title,body,category',[
 ('政府投资项目管理办法','政府投资项目应当开展后评价并追究违规责任。','incentive'),
 ('重大项目用地保障办法','优先保障重大项目新增建设用地，落实用地保障。','guarantee'),
 ('固定资产投资项目节能审查办法','固定资产投资项目必须开展节能审查。','access'),
])
def test_object_action_and_original_evidence(tmp_path,title,body,category):
    out=RuleClassifier(config(tmp_path).classification).classify(Document(title=title,content=body))
    assert out.is_investment_policy=='yes' and category in out.category
    assert out.evidence in body and out.need_review  # still a provisional rule result


def test_multi_label_needs_evidence_for_each_category(tmp_path):
    llm=LLMClassifier(config(tmp_path),{})
    text='优先保障重大项目用地。对政府投资项目开展后评价。'
    data=dict(is_investment_policy='yes',category=['guarantee','incentive'],doc_type='正式政策',
              evidence='优先保障重大项目用地',need_review=False,confidence=.95)
    out=llm._validate(Document(content=text),data)
    assert out.need_review and '独立证据' in out.reviewer_hint
    data['category_evidence']={'guarantee':'优先保障重大项目用地','incentive':'对政府投资项目开展后评价'}
    out=llm._validate(Document(content=text),data)
    assert not out.need_review and '分类证据' in out.reason
    data['category_evidence']['incentive']='编造证据'
    assert llm._validate(Document(content=text),data).need_review


def test_yunnan_encoded_pager_and_same_origin():
    page='<a href="/policies/2.html">下一页</a>'
    encoded=base64.b64encode(quote_plus(page).encode()).decode()
    raw=f'<div id="pages">{encoded}</div><script>window.atob(x)</script>'.encode()
    assert next_page_link(raw,'https://example.gov.cn/policies/')=='https://example.gov.cn/policies/2.html'
    assert not next_page_link('<a href="https://evil.test/">下一页</a>'.encode(),'https://example.gov.cn/')


def group(url, next_url=''):
    record=f'<record><![CDATA[<a href="{url}">政策通知</a>]]></record>' if url else ''
    return f'<datastore><nextgroup><![CDATA[<a href="{next_url}"></a>]]></nextgroup><recordset>{record}</recordset></datastore>'.encode()


def test_jpage_groups_and_partial_failure(tmp_path,monkeypatch):
    cfg=config(tmp_path);collector=Collector(cfg)
    src=SourceConfig(name='test',list_url='https://example.gov.cn/col/',include=['/art/'])
    proxy='/module/web/jpage/dataproxy.jsp?page=1'
    monkeypatch.setattr(collector,'fetch',lambda u:FetchResult(content=group('/art/2.html')))
    found=discover_jpage_links(collector,src,group('/art/1.html',proxy),src.list_url,3)
    assert len(found)==2 and collector.discovery_status['test']['stop_reason']=='no_next_group'
    assert not collector.discovery_status['test']['end_reached']
    monkeypatch.setattr(collector,'fetch',lambda u:FetchResult(ok=False,error='HTTP 403'))
    with pytest.raises(DiscoveryError) as e:discover_jpage_links(collector,src,group('/art/1.html',proxy),src.list_url,3)
    assert len(e.value.links)==1
    with pytest.raises(DiscoveryError,match='同源'):
        discover_jpage_links(collector,src,group('/art/1.html','https://evil.test/module/web/jpage/dataproxy.jsp'),src.list_url,3)
    collector.close()


def test_repeated_static_page_is_not_success(tmp_path,monkeypatch):
    cfg=config(tmp_path);src=SourceConfig(name='test',list_url='https://example.gov.cn/col/',include=['/art/'],pagination='trs',max_pages=3)
    cfg.sources={'test':src};pipe=Pipeline(cfg)
    try:
        pipe.sync_sources()
        monkeypatch.setattr(pipe.collector,'fetch',lambda u:FetchResult(content='<a href="/art/1.html">政策</a>'.encode()))
        stats=pipe.discover(src)
        assert stats.discovered==1 and stats.failed==1
        assert pipe.collector.discovery_status['test']['stop_reason']=='error'
        assert not pipe.db.get_source('test')['last_success_at']
    finally:pipe.close()


def test_yunnan_body_excludes_navigation():
    from policy_collector.parser import Parser
    raw='<meta charset="utf-8"><meta name="ArticleTitle" content="政策通知"><div>网站首页</div><div class="show-content">固定资产投资项目应当开展节能审查。</div><footer>网站地图</footer>'
    out=Parser().parse(raw.encode(),'html','https://yndrc.yn.gov.cn/html/2026/test/1.html')
    assert not out.parse_error and '网站地图' not in out.content and '网站首页' not in out.content


def test_provincial_registry_has_only_31_provinces():
    entries=json.loads((Path(__file__).resolve().parents[1]/'config/provinces.json').read_text())
    assert len(entries)==31 and len({e['region'] for e in entries})==31
    assert '杭州市' not in {e['region'] for e in entries}
