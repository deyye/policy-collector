"""用人工标注集评价已入库结果；不联网、不调用模型、不改写标签。
运行: python scripts/evaluate.py --db data/policy.db --gold gold.jsonl
每行: {"id":1,"relevant":true,"categories":["guarantee","incentive"]}
标注应独立于系统预测，待复核或规则回退预测按系统的 yes/no/pending 如实计数。
"""
import argparse
import json
import sqlite3
from pathlib import Path


def evaluate(db, labels):
    tp=fp=fn=ct=cp=cn=exact=pending=fallback=0
    seen=set()
    for gold in labels:
        if gold['id'] in seen: raise ValueError('标注 ID 重复')
        seen.add(gold['id'])
        if not isinstance(gold['relevant'],bool): raise ValueError('relevant 必须为布尔值')
        truth=set(gold['categories'])
        if not truth <= {'guide','access','guarantee','incentive'}: raise ValueError('非法分类标签')
        if not gold['relevant'] and truth: raise ValueError('非相关文件应无分类标签')
        row=db.execute('SELECT * FROM policies WHERE id=?',(gold['id'],)).fetchone()
        if row is None: raise ValueError(f"政策 ID 不存在: {gold['id']}")
        yes=row['is_investment_policy']=='yes'
        tp+=yes and gold['relevant'];fp+=yes and not gold['relevant'];fn+=not yes and gold['relevant']
        pending+=row['is_investment_policy']=='pending'
        fallback+=row['classification_method']=='rule_fallback'
        pred=set(filter(None,row['category'].split(','))) if yes else set()
        ct+=len(truth & pred);cp+=len(pred-truth);cn+=len(truth-pred)
        exact+=pred==truth and yes==gold['relevant'] and row['is_investment_policy']!='pending'
    def div(a,b):return round(a/b,4) if b else None
    return {'labeled_records':len(labels),'relevance_precision':div(tp,tp+fp),'relevance_recall':div(tp,tp+fn),
            'category_micro_precision':div(ct,ct+cp),'category_micro_recall':div(ct,ct+cn),
            'exact_match':div(exact,len(labels)),'pending':pending,'rule_fallback':fallback,
            'scope':'仅标注的已入库候选；不衡量官网发现召回率及被采集阶段排除的文件'}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True);parser.add_argument('--gold',required=True)
    args=parser.parse_args()
    con=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    try:
        labels=[json.loads(s) for s in Path(args.gold).read_text().splitlines() if s.strip()]
        if not labels:raise ValueError('标注集不能为空')
        print(json.dumps(evaluate(con,labels),ensure_ascii=False,indent=2))
    finally:con.close()
