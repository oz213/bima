"""Export human-confirmed pre-alert labels as append-free JSONL (no screenshots).

Only use for offline evaluation/training preparation; it NEVER changes a model.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def export_labels(db_path, out_path):
    db = Path(db_path).resolve()
    dst = Path(out_path).resolve()
    if not db.is_file() or db == dst:
        raise ValueError('invalid input/output database path')
    rows = 0
    uri = db.as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        with dst.open('w', encoding='utf-8') as output:
            for (cid, jid, url, final, bucket, raw, digest, label, who, when) in conn.execute(
                    'SELECT id,job_id,url,final_url,bucket,classifier_json,evidence_sha256,'
                    'feedback_label,reviewer_id,reviewed_at FROM bima_prealerts '
                    "WHERE feedback_label IN ('gambling','non_gambling') ORDER BY id"):
                try:
                    meta = json.loads(raw or '{}')
                except (ValueError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                row = {'schema':'bima_prealert_feedback_v1','case_id':cid,
                       'source_job_id':jid,'url':url,'final_url':final,'sample_bucket':bucket,
                       'label':label,'evidence_sha256':digest,'model_metadata':meta,
                       'reviewer':who,'reviewed_at':when}
                output.write(json.dumps(row, ensure_ascii=False)+'\n')
                rows += 1
    dst.chmod(0o600)
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Export confirmed BIMA Telegram review labels')
    parser.add_argument('--db', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print('Exported human labels:', export_labels(args.db,args.out))
