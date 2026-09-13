"""待办类型派生：把混装的 need_review 拆成"谁能解决"。

这些测试守三条不变式：
1. 优先级顺序（材料 > 系统 > 业务），因为它决定"谁来处理"
2. **材料判据是"有没有拿到正文"，不是 parse_status 是否等于 'ok'**——
   本条被四处各写一遍过，每次都把 partial 误算成缺失，实测让可用材料永远堵在队列里
3. SQL 版与 Python 版派生结果必须逐条一致（否则页面统计和列表过滤会对不上）
"""
import pytest

from policy_collector.config import AppConfig, SourceConfig
from policy_collector.pipeline import Pipeline
from policy_collector.todo import (
    MATERIAL, NONE, REVIEW, SYSTEM, TODO_META, TODO_ORDER,
    derive, document_incomplete, missing_text, sql_case,
)


@pytest.fixture
def pipe(tmp_path):
    cfg = AppConfig.load()
    cfg.db_path = tmp_path / 'policy.db'
    cfg.data_dir = tmp_path
    cfg.downloads_dir = tmp_path / 'downloads'
    cfg.sources = {'test': SourceConfig(name='test', site='合成官网', list_url='https://agency.gov.cn/policy/')}
    cfg.fetch.retries = 0
    cfg.fetch.request_interval_seconds = 0
    p = Pipeline(cfg)
    yield p
    p.close()


def seed(pipe, rows):
    """直接写库造样本：待办派生只关心这几个字段，不必走完整采集链路。"""
    with pipe.db.tx() as c:
        for i, (policy, attachments) in enumerate(rows, start=1):
            c.execute(
                'INSERT INTO policies(policy_key,version,title,is_investment_policy,need_review,'
                'review_status,parse_error,parse_requires_review,fallback_reason,region) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (f'k{i}', 1, policy.get('title', f'样本{i}'),
                 policy.get('is_investment_policy', 'yes'), int(policy.get('need_review', 1)),
                 policy.get('review_status', 'pending'), policy.get('parse_error', ''),
                 int(policy.get('parse_requires_review', 0)), policy.get('fallback_reason', ''),
                 policy.get('region', '甲')))
            pid = c.lastrowid
            for a in attachments:
                c.execute('INSERT INTO attachments(policy_id,name,parse_status,parsed_text) VALUES(?,?,?,?)',
                          (pid, a.get('name', 'a'), a.get('parse_status', 'ok'), a.get('parsed_text', '')))
    return pipe


# ---------- 1. 优先级顺序 ----------

def test_derive_priority_material_beats_system_and_review():
    """优先级即"谁来处理"：材料不全最优先（机器先修，修完结论才有意义）。

    若把模型故障排在材料之前，一条附件没下全的文件会被记成"运维问题"，
    运维配好接口后它照样判不出来——责任方错了。
    """
    both = {'parse_error': '', 'fallback_reason': '未配置可用的大模型接口', 'need_review': 1}
    assert derive(both, [{'parse_status': 'failed', 'parsed_text': ''}]) == MATERIAL
    assert derive(both, [{'parse_status': 'ok', 'parsed_text': '正文'}]) == SYSTEM
    assert derive({'need_review': 1, 'fallback_reason': ''}, []) == REVIEW
    assert derive({'need_review': 0}, []) == NONE


def test_derive_metadata_covers_every_type():
    """每个类型都要有中文名与责任方——页面直接用它渲染，缺了会露出英文键名。"""
    for key in (MATERIAL, SYSTEM, REVIEW, NONE):
        name, owner, note = TODO_META[key]
        assert name and owner and note
    assert set(TODO_ORDER) == {MATERIAL, SYSTEM, REVIEW}, '无需处理不进待办清单'


# ---------- 2. 材料判据：看有没有正文，不看 parse_status ----------

def test_partial_attachment_with_text_is_not_material():
    """`partial` 表示文本已提取、只是转换过程有提示，**不算材料缺失**。

    实测：旧版 doc 经 textutil 转换后 parse_status='partial' 但正文完整。
    若按 `parse_status != 'ok'` 判定，这类条目会永远挂在"材料待补"里——
    机器反复重试也修不掉，因为材料本来就是好的。
    """
    att = [{'parse_status': 'partial', 'parsed_text': '已成功提取的正文'}]
    assert derive({'need_review': 1}, att) == REVIEW
    assert not document_incomplete(type('D', (), {'parse_error': '', 'attachments': att})())


def test_attachment_without_text_is_material_regardless_of_status():
    """反过来：无论标成什么状态，只要拿不到正文就算材料待补。

    needs_ocr / unsupported / failed / 空字段——都不该因为"状态名看着还行"而放过去。
    """
    for status in ('failed', 'needs_ocr', 'unsupported', 'not_parsed', 'ok', ''):
        assert missing_text([{'parse_status': status, 'parsed_text': ''}])
        assert derive({'need_review': 1}, [{'parse_status': status, 'parsed_text': ''}]) == MATERIAL


def test_document_incomplete_is_the_single_judgement():
    """正文解析失败也算材料不完整（否则会带着 parse_error 去判结论）。"""
    D = type('D', (), {'parse_error': '旧解析器不支持', 'attachments': []})
    assert document_incomplete(D())
    assert document_incomplete(type('D', (), {'parse_error': '', 'attachments': []})()) is False


# ---------- 3. SQL 与 Python 必须一致 ----------

def test_sql_and_python_derivations_agree(pipe):
    """逐条比对两种派生。不一致就会"统计说 3 条、点进去只有 2 条"。"""
    seed(pipe, [
        ({'title': '附件没下全'}, [{'parse_status': 'failed', 'parsed_text': ''}]),
        ({'title': '附件已解析'}, [{'parse_status': 'ok', 'parsed_text': '正文'}]),
        ({'title': 'partial 有正文'}, [{'parse_status': 'partial', 'parsed_text': '正文'}]),
        ({'title': '模型故障', 'fallback_reason': '未配置可用的大模型接口'}, []),
        ({'title': '模型故障但有材料问题', 'fallback_reason': '超时'},
         [{'parse_status': 'needs_ocr', 'parsed_text': ''}]),
        ({'title': '正文解析失败', 'parse_error': '不支持'}, []),
        ({'title': '结论待定'}, []),
        ({'title': '明确通过', 'need_review': 0}, []),
        ({'title': '被剔除', 'review_status': 'rejected'}, []),
    ])
    rows = [dict(r) for r in pipe.db._conn.execute(
        f"""SELECT p.*, {sql_case()} AS sql_todo FROM policies p
            WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)
              AND p.review_status != 'rejected'""")]
    assert len(rows) == 8
    for r in rows:
        assert derive(r, pipe.db.list_attachments(r['id'])) == r['sql_todo'], r['title']
    got = {r['title']: r['sql_todo'] for r in rows}
    assert got['附件没下全'] == MATERIAL
    assert got['partial 有正文'] == REVIEW, 'partial 有正文不该算材料缺失'
    assert got['模型故障'] == SYSTEM
    assert got['模型故障但有材料问题'] == MATERIAL, '材料优先于故障'
    assert got['明确通过'] == NONE


def test_todo_overview_matches_filtered_list(pipe):
    """首页格子上的数字，必须等于点进去看到的条数。"""
    seed(pipe, [
        ({'title': 'A'}, [{'parse_status': 'failed', 'parsed_text': ''}]),
        ({'title': 'B'}, [{'parse_status': 'failed', 'parsed_text': ''}]),
        ({'title': 'C', 'fallback_reason': '未配置可用的大模型接口'}, []),
        ({'title': 'D'}, []),
        ({'title': 'E'}, []),
        ({'title': 'F', 'need_review': 0}, []),
    ])
    ov = pipe.db.todo_overview()
    assert ov['counts'][MATERIAL] == 2
    assert ov['counts'][SYSTEM] == 1
    assert ov['counts'][REVIEW] == 2
    assert ov['counts'][NONE] == 1
    # human 只算真正要人判断的：机器和运维的活不该算到人头上
    assert ov['human'] == 2 and ov['machine'] == 2 and ov['system'] == 1
    for key, expect in ((MATERIAL, 2), (SYSTEM, 1), (REVIEW, 2), (NONE, 1)):
        assert len(pipe.db.query_policies(todo=key, limit=100)) == expect, key
    # 格子顺序固定为 材料 → 系统 → 业务
    assert [c['key'] for c in ov['cells']] == list(TODO_ORDER)
    assert all('count' in c and 'owner' in c for c in ov['cells'])


def test_parse_requires_review_alone_is_not_material():
    """`parse_requires_review=1` 但附件正文齐全 → 归"结论待确认"，**不是**"材料待补"。

    这个字段的含义是"材料有**解析缺口**"：除了拿不到正文，还包括 `partial`
    ——正文已提取、但个别页是图形/模板（实测 OFD 报"第2页含图形/模板或缺少文本，
    需渲染核对"）。这类缺口**机器重试修不掉**（要靠渲染 + OCR），
    所以责任方不是机器，不能记到"机器自修"那一格。

    实测当前库有 20 条这种记录；若把它们算成"材料待补"，
    会让人以为机器在修一堆它根本修不了的东西，而真正待补的 32 条会被淹没。
    """
    assert derive({'need_review': 1, 'parse_requires_review': 1},
                  [{'parse_status': 'partial', 'parsed_text': '已提取的正文'}]) == REVIEW
    # 但确实没有正文时，仍然是材料待补
    assert derive({'need_review': 1, 'parse_requires_review': 1},
                  [{'parse_status': 'partial', 'parsed_text': ''}]) == MATERIAL


def test_sql_derivation_also_ignores_parse_requires_review(pipe):
    """SQL 版必须与 Python 版同口径：只看"有没有正文"。"""
    seed(pipe, [
        ({'title': 'partial 有正文但标记需核对', 'parse_requires_review': 1},
         [{'parse_status': 'partial', 'parsed_text': '正文'}]),
        ({'title': 'partial 无正文', 'parse_requires_review': 1},
         [{'parse_status': 'partial', 'parsed_text': ''}]),
    ])
    got = {r['title']: r['sql_todo'] for r in pipe.db._conn.execute(
        f"""SELECT p.title, {sql_case()} AS sql_todo FROM policies p
            WHERE p.version=(SELECT MAX(version) FROM policies v WHERE v.policy_key=p.policy_key)""")}
    assert got['partial 有正文但标记需核对'] == REVIEW
    assert got['partial 无正文'] == MATERIAL
