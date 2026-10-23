"""Local storage primitives: additive migrations, backups and atomic replacement."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import uuid

SCHEMA_VERSION = 1
_locks = threading.local()


def atomic_write_text(path: Path, text: str, expected=None) -> None:
    """Replace a whole UTF-8 file; optionally reject edits since it was read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None:
            actual = path.read_bytes() if path.exists() else b''
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError('文件已被并发编辑，保留原页，请重新读取后重建: ' + str(path))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def vault_lock(root: Path):
    """Serialize complete workflows, including derived page generation."""
    key = str(root.resolve())
    held = getattr(_locks, 'held', set())
    if key in held:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.write.lock'
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('此 Vault 正在写入；若上次进程已终止，请核实 .write.lock 后移除再重试') from exc
    held.add(key)
    _locks.held = held
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        held.remove(key)
        path.unlink(missing_ok=True)


def migrate_database(connection: sqlite3.Connection, backup_dir: Path) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('数据库版本高于当前处理器；请使用更新版本，不降级写入')
    if version == SCHEMA_VERSION:
        return
    has_data = sum(connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('transactions','debt_snapshots','source_files'))
    if has_data:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ('pre-v11-' + datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.sqlite3')
        with sqlite3.connect(backup) as destination:
            connection.backup(destination)
    # Do not use executescript here: it commits implicitly and breaks rollback.
    try:
        connection.execute('BEGIN IMMEDIATE')
        additions = {
            'active': 'INTEGER NOT NULL DEFAULT 1',
            'currency': "TEXT NOT NULL DEFAULT 'CNY'",
            'settlement': "TEXT NOT NULL DEFAULT 'unknown'",
            'source_reference': "TEXT NOT NULL DEFAULT ''",
            'source_locator': "TEXT NOT NULL DEFAULT ''",
            'transfer_scope': "TEXT NOT NULL DEFAULT 'unknown'",
            'category_manual': 'TEXT',
        }
        columns = {row[1] for row in connection.execute('PRAGMA table_info(transactions)')}
        for name, definition in additions.items():
            if name not in columns:
                connection.execute('ALTER TABLE transactions ADD COLUMN ' + name + ' ' + definition)
        connection.execute('ALTER TABLE debt_snapshots RENAME TO debt_snapshots_old')
        connection.execute('''CREATE TABLE debt_snapshots(snapshot_id TEXT PRIMARY KEY,account_name TEXT NOT NULL,as_of_date TEXT NOT NULL,
            outstanding_minor INTEGER,due_minor INTEGER,due_date TEXT NOT NULL,UNIQUE(account_name,as_of_date))''')
        connection.execute('INSERT INTO debt_snapshots SELECT * FROM debt_snapshots_old')
        connection.execute('DROP TABLE debt_snapshots_old')
        statements = [
            '''CREATE TABLE import_batches (batch_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, account_scope TEXT NOT NULL DEFAULT '',
               source_file TEXT NOT NULL, coverage TEXT NOT NULL, source_reference TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, receipt_json TEXT NOT NULL DEFAULT '{}', UNIQUE(source_hash,account_scope))''',
            '''CREATE TABLE transaction_observations (raw_id TEXT PRIMARY KEY REFERENCES raw_records(raw_id),
               transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
               batch_id TEXT NOT NULL REFERENCES import_batches(batch_id))''',
            '''CREATE TABLE decisions (decision_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
               target_id TEXT NOT NULL, value_json TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''',
            '''CREATE TABLE refund_rejections (refund_id TEXT NOT NULL, purchase_id TEXT NOT NULL,
               PRIMARY KEY(refund_id,purchase_id))''',
            '''CREATE TABLE category_rules (rule_id INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL,
               category TEXT NOT NULL, effective_from TEXT NOT NULL)''',
            '''CREATE TABLE report_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
               status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''',
            "INSERT INTO report_state VALUES (1,'pending','')",
            'CREATE INDEX tx_matching ON transactions(active,transaction_type,source_platform,account_name,amount_minor,transaction_time)',
            'CREATE INDEX observations_tx ON transaction_observations(transaction_id,batch_id)',
            'CREATE INDEX refund_purchase ON refund_links(purchase_id)',
        ]
        for statement in statements:
            connection.execute(statement)
        # Every V1 row remains addressable. Merging legacy overlapping IDs is
        # represented by an exclusion, never a destructive row rewrite.
        rows = connection.execute('SELECT t.*, r.source_hash, r.source_file FROM transactions t JOIN raw_records r ON r.raw_id=t.raw_id ORDER BY t.transaction_id').fetchall()
        names = [item[0] for item in connection.execute('SELECT t.*, r.source_hash, r.source_file FROM transactions t JOIN raw_records r ON r.raw_id=t.raw_id LIMIT 0').description]
        canonical = {}
        for values in rows:
            row = dict(zip(names, values))
            batch = hashlib.sha256(('legacy:' + row['source_hash']).encode()).hexdigest()[:24]
            connection.execute('INSERT OR IGNORE INTO import_batches(batch_id,source_hash,source_file,coverage,created_at) VALUES(?,?,?,?,?)',
                               (batch, row['source_hash'], row['source_file'], 'unknown', datetime.now().isoformat()))
            connection.execute('INSERT OR IGNORE INTO transaction_observations VALUES(?,?,?)', (row['raw_id'], row['transaction_id'], batch))
            if row['account_name'] and row['platform_transaction_id']:
                key = (row['source_platform'], row['account_name'], row['account_last4'], row['platform_transaction_id'])
                if key in canonical:
                    other = canonical[key]
                    if all(row[k] == other[k] for k in ('amount_minor','direction','transaction_type','transaction_time','merchant_raw')):
                        connection.execute("UPDATE transactions SET excluded_reason='identity_duplicate', duplicate_of=? WHERE transaction_id=?", (other['transaction_id'], row['transaction_id']))
                        connection.execute('UPDATE transaction_observations SET transaction_id=? WHERE raw_id=?', (other['transaction_id'], row['raw_id']))
                        # Alias migration preserves the same economic event and
                        # records the old stable ID; it is not a new user choice.
                        connection.execute("UPDATE refund_links SET purchase_id=? WHERE purchase_id=?",(other['transaction_id'],row['transaction_id']))
                        if row['transaction_type']=='refund':
                            old_link=connection.execute('SELECT purchase_id,matched_by,confidence FROM refund_links WHERE refund_id=?',(row['transaction_id'],)).fetchone()
                            existing_link=connection.execute('SELECT purchase_id,matched_by,confidence FROM refund_links WHERE refund_id=?',(other['transaction_id'],)).fetchone()
                            if old_link:
                                if existing_link and existing_link[1]=='manual' and old_link[1]=='manual' and existing_link[0]!=old_link[0]:
                                    # Two conflicting V1 choices for one event:
                                    # keep both in audit and pause automation.
                                    connection.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('migration_refund_conflict',?,?,?)",(other['transaction_id'],json.dumps(dict(original=list(old_link),canonical=list(existing_link))),datetime.now().isoformat()))
                                    connection.execute('DELETE FROM refund_links WHERE refund_id=?',(other['transaction_id'],))
                                elif not existing_link or old_link[1]=='manual':
                                    connection.execute('INSERT OR REPLACE INTO refund_links VALUES(?,?,?,?)',(other['transaction_id'],old_link[0],old_link[1],old_link[2]))
                                connection.execute('DELETE FROM refund_links WHERE refund_id=?',(row['transaction_id'],))

                        connection.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('migration_alias',?,?,?)",(row['transaction_id'],json.dumps(dict(canonical=other['transaction_id'])),datetime.now().isoformat()))

                else:
                    canonical[key] = row
        connection.execute('PRAGMA user_version = ' + str(SCHEMA_VERSION))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
