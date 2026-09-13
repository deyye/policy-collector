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


def test_run_all_sources_isolates_single_source_failure(tmp_path, monkeypatch):
    """一键全国采集：单源失败必须被隔离，不能让后面 20 个省一并不采。

    这是这个功能存在的唯一理由。若某个省 403/超时就把整批打断，
    那"一键"反而比逐个点更糟——人得盯着哪一站挂了再手动补跑。
    """
    cfg=config(tmp_path)
    cfg.sources={
        'a_ok': SourceConfig(name='a_ok',site='甲省发改委',region='甲',enabled=True,
                             list_url='https://a.gov.cn/zc/',include=['/zc/']),
        'b_broken': SourceConfig(name='b_broken',site='乙省发改委',region='乙',enabled=True,
                             list_url='https://b.gov.cn/zc/',include=['/zc/']),
        'c_ok': SourceConfig(name='c_ok',site='丙省发改委',region='丙',enabled=True,
                             list_url='https://c.gov.cn/zc/',include=['/zc/']),
    }
    call_order=[]
    def fake_run_source(self, name, prefer='llm', limit=50, retry_only=False, reclassify=False):
        from policy_collector.pipeline import RunStats
        call_order.append(name)
        if name=='b_broken':
            raise RuntimeError('HTTP 403')
        s=RunStats();s.discovered=1;s.ingested=1;return s
    monkeypatch.setattr(Pipeline,'run_source',fake_run_source)
    pipe=Pipeline(cfg)
    try:
        out=pipe.run_all_sources(prefer='rule',limit=10,pause_seconds=0)
        # 关键断言：三个来源都被尝试过，失败的那个没有中断整批
        assert call_order==['a_ok','b_broken','c_ok']
        assert out['total']==3
        by={r['source']:r for r in out['results']}
        assert by['a_ok']['status']=='ok' and by['c_ok']['status']=='ok'
        assert by['b_broken']['status']=='failed' and '403' in by['b_broken']['error']
        assert out['status']=='partial' and out['stats']['ingested']==2
        # 批次记录必须存在且已收尾，进度里能按来源追责
        row=pipe.db.get_run_summary(out['run_id'])
        assert row['kind']=='batch' and row['status']=='partial'
        progress=json.loads(row['progress'])
        assert progress['completed']==3 and len(progress['results'])==3
        assert {r['source'] for r in progress['results']}=={'a_ok','b_broken','c_ok'}
    finally:
        pipe.close()


def test_batch_run_is_not_killed_by_stale_run_cleanup(tmp_path, monkeypatch):
    """批次行不能被 run_source 的"清理上次中断"误标为失败。

    每个来源启动时都会把 status='running' 的行标失败（防上次中断留脏）。
    若不排除当前批次自身，批次会在第一个来源启动瞬间变成 failed。
    """
    cfg=config(tmp_path)
    cfg.sources={'a_ok': SourceConfig(name='a_ok',site='甲省发改委',region='甲',enabled=True,
                                      list_url='https://a.gov.cn/zc/',include=['/zc/'])}
    pipe=Pipeline(cfg)
    try:
        captured={}
        real=Pipeline.run_source
        def spy(self, name, prefer='llm', limit=50, retry_only=False, reclassify=False):
            captured['batch_status_during_run']=self.db.get_run_summary(self.active_batch)['status']
            from policy_collector.pipeline import RunStats
            return RunStats()
        monkeypatch.setattr(Pipeline,'run_source',spy)
        out=pipe.run_all_sources(prefer='rule',limit=5,pause_seconds=0)
        assert captured['batch_status_during_run']=='running'
        assert pipe.db.get_run_summary(out['run_id'])['status']=='ok'
    finally:
        pipe.close()


def test_audit_flags_national_repost_and_ignores_date_dirs():
    """验收脚本的归属检查：聚合栏目下的中央转载要能被抓出来，年月目录不能误报。

    背景：福建 /zwgk/fgzd/ 是聚合页，前 15 条全在 gjfgwwj（国家发改委文件转载）子目录下，
    收进来会把中央文件的 region 错记成福建——而机制检查（列表/正文/附件/入库）全过。
    这条检查把"归属错记"从只能人工发现，变成脚本能判。
    """
    from scripts.audit_sources import sub_column_distribution, NATIONAL_REPOST
    # 常规 TRS 站：栏目下第一段是年月，不是子栏目 —— 不能误报
    normal = ['https://x.gov.cn/zcfb/ghxwj/202604/t20260430_1.html',
              'https://x.gov.cn/zcfb/ghxwj/202601/t20260109_2.html',
              'https://x.gov.cn/zcfb/ghxwj/2026/t20260109_3.html']
    assert sub_column_distribution(normal, 'https://x.gov.cn/zcfb/ghxwj/') == {}
    # 聚合页：并列多个子栏目，其中一个是中央转载
    mixed = ['https://x.gov.cn/zwgk/fgzd/gjfgwwj/202609/t1.htm',
             'https://x.gov.cn/zwgk/fgzd/gjfgwwj/202608/t2.htm',
             'https://x.gov.cn/zwgk/fgzd/sfgwgfxwj/202609/t3.htm']
    dist = sub_column_distribution(mixed, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'gjfgwwj': 2, 'sfgwgfxwj': 1}
    assert [k for k in dist if NATIONAL_REPOST.search(k)] == ['gjfgwwj']


def test_audit_sub_column_distribution_marks_out_of_column_links():
    """详情跳到栏目路径之外的也要标出来（跨栏目/跨站混杂的另一种形态）。"""
    from scripts.audit_sources import sub_column_distribution
    urls = ['https://x.gov.cn/other/section/t1.html',
            'https://x.gov.cn/zwgk/fgzd/sub/t2.htm']
    dist = sub_column_distribution(urls, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'(栏目路径之外)': 1, 'sub': 1}


def test_audit_flags_national_repost_and_ignores_date_dirs():
    """验收脚本的归属检查：聚合栏目下的中央转载要能被抓出来，年月目录不能误报。

    背景：福建 /zwgk/fgzd/ 是聚合页，前 15 条全在 gjfgwwj（国家发改委文件转载）子目录下，
    收进来会把中央文件的 region 错记成福建——而机制检查（列表/正文/附件/入库）全过。
    这条检查把"归属错记"从只能人工发现，变成脚本能判。
    """
    from scripts.audit_sources import sub_column_distribution, NATIONAL_REPOST
    # 常规 TRS 站：栏目下第一段是年月，不是子栏目 —— 不能误报
    normal = ['https://x.gov.cn/zcfb/ghxwj/202604/t20260430_1.html',
              'https://x.gov.cn/zcfb/ghxwj/202601/t20260109_2.html',
              'https://x.gov.cn/zcfb/ghxwj/2026/t20260109_3.html']
    assert sub_column_distribution(normal, 'https://x.gov.cn/zcfb/ghxwj/') == {}
    # 聚合页：并列多个子栏目，其中一个是中央转载
    mixed = ['https://x.gov.cn/zwgk/fgzd/gjfgwwj/202609/t1.htm',
             'https://x.gov.cn/zwgk/fgzd/gjfgwwj/202608/t2.htm',
             'https://x.gov.cn/zwgk/fgzd/sfgwgfxwj/202609/t3.htm']
    dist = sub_column_distribution(mixed, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'gjfgwwj': 2, 'sfgwgfxwj': 1}
    assert [k for k in dist if NATIONAL_REPOST.search(k)] == ['gjfgwwj']


def test_audit_sub_column_distribution_marks_out_of_column_links():
    """详情跳到栏目路径之外的也要标出来（跨栏目/跨站混杂的另一种形态）。"""
    from scripts.audit_sources import sub_column_distribution
    urls = ['https://x.gov.cn/other/section/t1.html',
            'https://x.gov.cn/zwgk/fgzd/sub/t2.htm']
    dist = sub_column_distribution(urls, 'https://x.gov.cn/zwgk/fgzd/')
    assert dist == {'(栏目路径之外)': 1, 'sub': 1}
