"""用真实大模型对标注集(gold)重分类，输出 规则 vs LLM 双基线评测对比。

背景：policies 表里的预测是 rule 模式入库的（classification_method='rule'）。
本脚本从隔离库复制出评测副本，仅对 gold.jsonl 中的 id 用已配置 LLM 重分类
并写回副本库，然后对同一份 gold 分别跑 evaluate（规则预测取自原库快照，
LLM 预测取自副本），输出对比表与逐条差异。

用法:
    python scripts/eval_llm_compare.py \
        --src-db /tmp/pc_zj_full/policy.db \
        --gold gold/zj_v1_20260909/gold.jsonl \
        --out-dir /tmp/pc_llm_eval

只读原库；LLM 预测写副本库，不动任何正式数据。需先配置真实模型（configure-llm 或 .env）。
"""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from policy_collector.config import AppConfig
from policy_collector.classifier import Classifier
from policy_collector.models import Document

CATEGORY_KEYS = ('guide', 'access', 'guarantee', 'incentive')


def load_gold(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]


def evaluate_db(con: sqlite3.Connection, labels: list[dict]) -> dict:
    """与 scripts/evaluate.py 相同的指标口径（内联，避免 import CLI）。"""
    tp = fp = fn = ct = cp = cn = exact = pending = fallback = 0
    for gold in labels:
        truth = set(gold['categories'])
        row = con.execute('SELECT * FROM policies WHERE id=?', (gold['id'],)).fetchone()
        if row is None:
            raise ValueError(f"id 不存在: {gold['id']}")
        yes = row['is_investment_policy'] == 'yes'
        tp += yes and gold['relevant']
        fp += yes and not gold['relevant']
        fn += (not yes) and gold['relevant']
        pending += row['is_investment_policy'] == 'pending'
        fallback += row['classification_method'] == 'rule_fallback'
        pred = set(filter(None, (row['category'] or '').split(','))) if yes else set()
        ct += len(truth & pred)
        cp += len(pred - truth)
        cn += len(truth - pred)
        exact += pred == truth and yes == gold['relevant'] and row['is_investment_policy'] != 'pending'

    def div(a, b):
        return round(a / b, 4) if b else None

    return {
        'labeled_records': len(labels),
        'relevance_precision': div(tp, tp + fp),
        'relevance_recall': div(tp, tp + fn),
        'category_micro_precision': div(ct, ct + cp),
        'category_micro_recall': div(ct, ct + cn),
        'exact_match': div(exact, len(labels)),
        'pending': pending,
        'rule_fallback': fallback,
    }


def reclassify_with_llm(src_con: sqlite3.Connection, copy_db: Path, labels: list[dict],
                        classifier: Classifier, out_dir: Path) -> dict:
    """复制源库→对 gold id 用 LLM 重分类写回副本→记录逐条差异。"""
    if copy_db.exists():
        copy_db.unlink()
    shutil.copy2(src_con_db_path, copy_db)

    con = sqlite3.connect(copy_db)
    con.row_factory = sqlite3.Row
    diffs = []
    ok = fail = 0
    for g in labels:
        row = con.execute('SELECT * FROM policies WHERE id=?', (g['id'],)).fetchone()
        if row is None:
            continue
        doc = Document(
            page_url=row['page_url'] or '',
            title=row['title'] or '',
            wenhao=row['wenhao'] or '',
            issuing_authority=row['issuing_authority'] or '',
            page_date=row['page_date'] or '',
            doc_date=row['doc_date'] or '',
            content=row['content'] or '',
        )
        old_pred = {'is_investment_policy': row['is_investment_policy'],
                    'category': row['category'] or '', 'method': row['classification_method']}
        try:
            cls = classifier.classify(doc, prefer='llm')
        except Exception as e:  # 网络/解析错误不中断，记录后继续
            diffs.append({'id': g['id'], 'error': str(e)[:200]})
            fail += 1
            continue
        with con:
            con.execute('''UPDATE policies SET is_investment_policy=?, category=?, category_names=?,
                           doc_type=?, need_review=?, reason=?, evidence=?, confidence=?,
                           model_version=?, reviewer_hint=?, classification_method=?, fallback_reason=?,
                           input_tokens=?, output_tokens=? WHERE id=?''',
                        (cls.is_investment_policy, cls.category, cls.category_names, cls.doc_type,
                         int(cls.need_review), cls.reason, cls.evidence, cls.confidence,
                         cls.model_version, cls.reviewer_hint, cls.method, cls.fallback_reason,
                         cls.input_tokens, cls.output_tokens, g['id']))
        ok += 1
        new_pred = {'is_investment_policy': cls.is_investment_policy, 'category': cls.category,
                    'method': cls.method}
        if new_pred != old_pred:
            diffs.append({'id': g['id'],
                          'gold_relevant': g['relevant'], 'gold_categories': g['categories'],
                          'rule': old_pred, 'llm': new_pred,
                          'llm_reason': (cls.reason or '')[:160]})
    con.close()
    (out_dir / 'reclassify_diffs.jsonl').write_text(
        '\n'.join(json.dumps(d, ensure_ascii=False) for d in diffs), encoding='utf-8')
    return {'llm_classified': ok, 'failed': fail, 'changed': sum(1 for d in diffs if 'error' not in d)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src-db', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--out-dir', default='/tmp/pc_llm_eval')
    args = ap.parse_args()

    global src_con_db_path
    src_con_db_path = str(Path(args.src_db).resolve())

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    labels = load_gold(Path(args.gold))
    cfg = AppConfig.load()
    if not cfg.llm.enabled or not cfg.llm.api_key:
        sys.exit('未配置真实模型（llm.enabled/api_key 缺失），先运行 configure-llm 或设置 .env')
    classifier = Classifier(cfg)

    # 原库(rule 预测)评测
    src_con = sqlite3.connect(src_con_db_path)
    src_con.row_factory = sqlite3.Row
    rule_metrics = evaluate_db(src_con, labels)

    # LLM 重分类副本评测
    copy_db = out_dir / 'policy_llm.db'
    llm_stat = reclassify_with_llm(src_con, copy_db, labels, classifier, out_dir)
    llm_con = sqlite3.connect(copy_db)
    llm_con.row_factory = sqlite3.Row
    llm_metrics = evaluate_db(llm_con, labels)
    llm_con.close()

    report = {
        'model': cfg.llm.effective_model,
        'labeled': len(labels),
        'rule_baseline': rule_metrics,
        'llm': llm_metrics,
        'llm_reclassify': llm_stat,
    }
    (out_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    src_con.close()


if __name__ == '__main__':
    main()
