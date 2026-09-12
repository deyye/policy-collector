from pathlib import Path
from types import SimpleNamespace
import pytest
from policy_collector.config import AppConfig, SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.models import Classification
from policy_collector.webapp import create_app

BODY='投资项目给予资金支持，明确监督管理规则。项目资金用于基础设施建设，申报单位应当提供完整材料，严格履行审批程序，接受全过程监督和绩效评价，确保项目规范实施。'
RAW=('<meta charset="utf-8"><h1>投资项目支持办法</h1><article>'+BODY+'</article>').encode()
URL='https://agency.gov.cn/policy/1.html'

@pytest.fixture
def pipe(tmp_path):
    cfg=AppConfig.load();cfg.data_dir=tmp_path;cfg.db_path=tmp_path/'db';cfg.downloads_dir=tmp_path/'raw';cfg.llm.enabled=False
    cfg.sources={'test':SourceConfig(name='test',list_url='https://agency.gov.cn/policy/')}
    p=Pipeline(cfg)
    yield p
    p.close()


def ingest(p):return p.ingest_url(p.cfg.sources['test'],URL,raw=RAW)

@pytest.mark.parametrize('review,expected',[(True,'pending'),(False,'excluded')])
def test_negative_review_is_retained(pipe,review,expected):
    pipe.classifier.classify=lambda *a:Classification(is_investment_policy='no',need_review=review,method='llm')
    stats=ingest(pipe)
    assert stats.excluded==int(not review)
    if review:assert pipe.db.query_policies(review_status=expected)[0]['is_investment_policy']=='pending'
    else:assert pipe.db.list_fetch()[0]['status']=='excluded'

@pytest.mark.parametrize('human',[False,True])
def test_model_recovers_once_without_overwriting_human(pipe,human):
    ingest(pipe);row=pipe.db.query_policies()[0]
    if human:pipe.db.audit(row['id'],'adjust',['guarantee'])
    calls=[]
    pipe.classifier.llm=SimpleNamespace(available=True)
    def classify(*a):
        calls.append(1)
        return Classification(is_investment_policy='yes',category='guarantee',method='llm',need_review=False)
    pipe.classifier.classify=classify
    first=ingest(pipe);ingest(pipe)
    assert len(calls)==(0 if human else 1)
    assert first.reclassified==int(not human)
    if human:assert pipe.db.get_policy(row['id'])['review_status']=='adjusted'


def test_missing_model_does_not_retry_unchanged_document(pipe):
    ingest(pipe)
    pipe.classifier.classify=lambda *a:pytest.fail('Unavailable model should not reclassify unchanged records')
    assert ingest(pipe).duplicates==1


def test_search_attachment_and_web_excerpt(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.add_attachment(row['id'],dict(name='资金支持附件',url='https://agency.gov.cn/a.txt',parsed_text='符合条件的项目给予贴息支持。',parse_status='ok'))
    found=pipe.db.query_policies(keyword='贴息')
    assert len(found)==1 and found[0]['matched_attachment']=='资金支持附件'
    client=create_app(pipe.cfg).test_client()
    html=client.get('/policies?q=贴息').get_data(as_text=True)
    assert '命中附件：资金支持附件' in html and '给予贴息支持' in html
    assert not pipe.db.query_policies(keyword='不存在的关键词')

@pytest.mark.parametrize('action',['confirm','adjust'])
def test_business_review_keeps_material_warning(pipe,action):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.update_policy(row['id'],parse_error='正文尚不完整')
    pipe.db.audit(row['id'],action,['guarantee'],'已核对分类，等待补采')
    current=pipe.db.get_policy(row['id'])
    assert current['need_review']==current['parse_requires_review']==1
    assert current['review_status'] in ('confirmed','adjusted')
    assert pipe.db.query_policies(review_status='pending')
    pipe.db.update_policy(row['id'],parse_error='')
    pipe.db.audit(row['id'],'confirm')
    assert pipe.db.get_policy(row['id'])['parse_requires_review']==0


def test_attachment_failure_cannot_be_cleared_by_confirm(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.add_attachment(row['id'],dict(name='缺失附件',url='https://agency.gov.cn/missing.pdf',parse_status='failed'))
    pipe.db.audit(row['id'],'confirm')
    assert pipe.db.get_policy(row['id'])['parse_requires_review']==1


def test_reopen_confirmed_and_rejected_with_note_and_csrf(pipe):
    ingest(pipe);row=pipe.db.query_policies()[0]
    pipe.db.audit(row['id'],'confirm')
    client=create_app(pipe.cfg).test_client()
    for action in ('reject','adjust'):
        html=client.get(f"/policies/{row['id']}").get_data(as_text=True)
        assert '重新复核 / 修改分类' in html and 'name="note"' in html
        with client.session_transaction() as sess:token=sess['csrf']
        response=client.post(f"/policies/{row['id']}/review",data={'csrf_token':token,'action':action,'categories':['incentive'],'note':'重新核对：应按项目监管条款分类'})
        assert response.status_code==302
        assert pipe.db.review_history(row['id'])[0]['note']=='重新核对：应按项目监管条款分类'
    assert pipe.db.get_policy(row['id'])['category']=='incentive'
