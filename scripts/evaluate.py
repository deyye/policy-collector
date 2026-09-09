"""离线评价已入库结果；AI 初标仅用于探索，不作为人工验收成绩。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_collector.evaluation import evaluate_db as evaluate, load_gold, readonly_db, label_summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', required=True)
    ap.add_argument('--gold', required=True)
    ap.add_argument('--allow-provisional', action='store_true', help='允许未终审标签，报告标记 exploratory')
    args = ap.parse_args()
    try:
        labels = load_gold(Path(args.gold))
        verified = label_summary(labels)['human_reviewed'] == len(labels)
        if not verified and not args.allow_provisional:
            raise ValueError('标注尚未全部人工终审；探索性比较请显式加 --allow-provisional')
        con = readonly_db(Path(args.db))
        try:
            report = evaluate(con, labels)
            report['evaluation_status'] = 'human_reviewed' if verified else 'exploratory'
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            con.close()
    except (ValueError, OSError, sqlite3.Error) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
