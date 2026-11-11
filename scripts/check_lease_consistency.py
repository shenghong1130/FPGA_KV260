"""Read-only SQLite ownership audit; exits 1 if inconsistencies exist.

python3 scripts/check_lease_consistency.py --database server/data/central.db
Does not initialize the application, migrate the DB, or contact Worker APIs.
"""
import argparse
import json
from pathlib import Path
import sqlite3


def check(path: Path) -> dict:
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        queries = {
            'orphan_workers': '''SELECT board,state,session_id FROM workers w
                WHERE session_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM student_leases s WHERE s.lease_id=w.session_id AND s.worker_id=w.board)''',
            'broken_student_links': '''SELECT student_id,state,worker_id,lease_id FROM student_leases s
                WHERE (lease_id IS NOT NULL OR worker_id IS NOT NULL) AND NOT EXISTS (
                SELECT 1 FROM workers w WHERE w.session_id=s.lease_id AND w.board=s.worker_id)''',
            'duplicate_worker_owners': '''SELECT worker_id,count(*) FROM student_leases
                WHERE worker_id IS NOT NULL GROUP BY worker_id HAVING count(*)>1''',
            'unsafe_idle_workers': '''SELECT board,session_id,fpga_ready FROM workers
                WHERE state='IDLE' AND (session_id IS NOT NULL OR fpga_ready!=0)''',
        }
        return {name: connection.execute(sql).fetchall() for name,sql in queries.items()}
    finally:
        connection.rollback()
        connection.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    args=parser.parse_args()
    result=check(args.database)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(1 if any(result.values()) else 0)
