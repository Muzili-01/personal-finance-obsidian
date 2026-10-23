import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import finance as f


class StorageTests(unittest.TestCase):
    def test_atomic_replace_failure_preserves_old_file(self):
        import finance_storage as storage
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            path = Path(directory) / 'report.md'
            path.write_text('完整旧页', encoding='utf-8')
            with patch.object(storage.os, 'replace', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    storage.atomic_write_text(path, '新页')
            self.assertEqual(path.read_text(), '完整旧页')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_old_database_migration_backs_up_and_preserves_manual_link(self):
        import finance_storage as storage
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            db = Path(directory) / 'old.sqlite3'
            con = sqlite3.connect(db)
            con.executescript(f.SCHEMA)
            con.execute("INSERT INTO raw_records VALUES ('raw', 'synthetic.csv', 'hash', 2, '{}')")
            for tid, kind in [('p', 'purchase'), ('r', 'refund')]:
                con.execute('''INSERT INTO transactions (transaction_id,raw_id,transaction_time,source_platform,
                    account_name,merchant_raw,description,amount_minor,direction,transaction_type,
                    payment_method,status,platform_transaction_id,merchant_order_id,account_last4)
                    VALUES (?, 'raw','2026-07-01','alipay','示例','商店','',10000,'expense',?,'','成功',?,'','')''', (tid, kind, tid))
            con.execute("INSERT INTO refund_links VALUES ('r','p','manual',1)")
            con.commit()
            storage.migrate_database(con, Path(directory) / 'backups')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT matched_by FROM refund_links').fetchone()[0], 'manual')
            self.assertGreater(con.execute('PRAGMA user_version').fetchone()[0], 0)
            backups = list((Path(directory) / 'backups').glob('*.sqlite3'))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as old:
                self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(old.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
            con.close()

    def test_report_failure_keeps_old_markdown_and_marks_ledger_for_rebuild(self):
        from test_finance_v11 import FinanceV11Tests
        import finance_storage as storage
        case=FinanceV11Tests();case.setUp()
        try:
            f.import_file(case.vault,case.source('one.csv',[case.row()]))
            f.build_all_months(case.vault)
            path=f.finance_root(case.vault)/'Months/2026-07.md'
            old=path.read_bytes()
            with patch.object(storage.os,'replace',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):
                    f.build_all_months(case.vault)
            self.assertEqual(path.read_bytes(),old)
            with f.connect(case.vault) as con:
                self.assertEqual(con.execute('SELECT status FROM report_state').fetchone()[0],'failed')
            f.build_all_months(case.vault)
            with f.connect(case.vault) as con:
                self.assertEqual(con.execute('SELECT status FROM report_state').fetchone()[0],'current')
        finally:
            case.doCleanups()

    def test_migration_failure_rolls_back_schema_and_keeps_backup(self):
        import finance_storage as storage
        class FailConnection(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if sql.startswith('CREATE INDEX tx_matching'):
                    raise sqlite3.OperationalError('synthetic migration failure')
                return super().execute(sql,*args,**kwargs)
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            db=Path(directory)/'old.sqlite3'
            con=sqlite3.connect(db,factory=FailConnection)
            con.executescript(f.SCHEMA)
            con.execute("INSERT INTO debt_snapshots VALUES('s','示例','2026-07-31',10000,0,'')");con.commit()
            with self.assertRaises(sqlite3.OperationalError):
                storage.migrate_database(con,Path(directory)/'backups')
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],0)
            self.assertNotIn('active',{r[1] for r in con.execute('PRAGMA table_info(transactions)')})
            self.assertEqual(con.execute('SELECT outstanding_minor FROM debt_snapshots').fetchone()[0],10000)
            self.assertEqual(len(list((Path(directory)/'backups').glob('*.sqlite3'))),1)
            con.close()

    def test_legacy_identity_duplicate_keeps_manual_refund_economically_linked(self):
        import json
        from test_finance_v11 import FinanceV11Tests
        case=FinanceV11Tests();case.setUp()
        try:
            db=f.database_path(case.vault);db.unlink()
            with sqlite3.connect(db) as con:
                con.executescript(f.SCHEMA)
                for tid,kind,pid in [('a','purchase','P'),('z','purchase','P'),('r','refund','R')]:
                    row=case.refund(platform_transaction_id=pid) if kind=='refund' else case.row(platform_transaction_id=pid)
                    con.execute('INSERT INTO raw_records VALUES(?,?,?,?,?)',(tid,'synthetic.csv',tid,2,json.dumps(row)))
                    con.execute('''INSERT INTO transactions(transaction_id,raw_id,transaction_time,source_platform,account_name,merchant_raw,description,
                        amount_minor,direction,transaction_type,payment_method,status,platform_transaction_id,merchant_order_id,account_last4)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(tid,tid,row['transaction_date'],row['source_platform'],row['account_name'],row['merchant'],row['description'],10000,row['direction'],kind,row['payment_method'],row['status'],pid,'',''))
                con.execute("INSERT INTO refund_links VALUES('r','z','manual',1)")
            ctx=case.context()
            self.assertEqual(ctx['metrics']['total_consumption'],'0.00')
            with f.connect(case.vault) as con:
                link=con.execute('SELECT purchase_id,matched_by FROM refund_links').fetchone()
                self.assertEqual(tuple(link),('a','manual'))
                self.assertEqual(con.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],3)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM decisions WHERE kind='migration_alias'").fetchone()[0],0)
        finally:
            case.doCleanups()

    def test_receipt_write_failure_explains_committed_ledger_and_rebuild_recovers(self):
        import finance_storage as storage
        from test_finance_v11 import FinanceV11Tests
        case=FinanceV11Tests();case.setUp()
        try:
            source=case.source('receipt-failure.csv',[case.row()])
            with patch.object(storage.os,'replace',side_effect=OSError('synthetic receipt failure')):
                with self.assertRaisesRegex(ValueError,'总账已更新'):
                    f.import_with_receipt(case.vault,source)
            with f.connect(case.vault) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],1)
                batch=con.execute('SELECT batch_id FROM import_batches').fetchone()[0]
                self.assertEqual(con.execute('SELECT status FROM report_state').fetchone()[0],'failed')
            f.build_all_months(case.vault)
            receipt=f.finance_root(case.vault)/'Imports'/(batch+'.json')
            self.assertTrue(receipt.exists())
        finally:
            case.doCleanups()

    def test_atomic_write_rejects_concurrent_edit_instead_of_overwriting(self):
        import hashlib
        import finance_storage as storage
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            path=Path(directory)/'notes.md';path.write_text('old')
            expected=hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_text('user edit')
            with self.assertRaisesRegex(ValueError,'编辑'):
                storage.atomic_write_text(path,'generated',expected=expected)
            self.assertEqual(path.read_text(),'user edit')

    def test_legacy_duplicate_refund_preserves_manual_choice_over_ambiguous_automation(self):
        import json
        from test_finance_v11 import FinanceV11Tests
        case=FinanceV11Tests();case.setUp()
        try:
            db=f.database_path(case.vault);db.unlink()
            with sqlite3.connect(db) as con:
                con.executescript(f.SCHEMA)
                for tid,kind,pid in [('p1','purchase','P1'),('p2','purchase','P2'),('ra','refund','R'),('rz','refund','R')]:
                    row=case.refund(platform_transaction_id=pid) if kind=='refund' else case.row(platform_transaction_id=pid)
                    con.execute('INSERT INTO raw_records VALUES(?,?,?,?,?)',(tid,'synthetic.csv',tid,2,json.dumps(row)))
                    con.execute('''INSERT INTO transactions(transaction_id,raw_id,transaction_time,source_platform,account_name,merchant_raw,description,
                        amount_minor,direction,transaction_type,payment_method,status,platform_transaction_id,merchant_order_id,account_last4)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(tid,tid,row['transaction_date'],row['source_platform'],row['account_name'],row['merchant'],row['description'],10000,row['direction'],kind,row['payment_method'],row['status'],pid,'',''))
                con.execute("INSERT INTO refund_links VALUES('rz','p2','manual',1)")
            self.assertEqual(case.context()['metrics']['total_consumption'],'100.00')
            with f.connect(case.vault) as con:
                self.assertEqual(tuple(con.execute('SELECT refund_id,purchase_id,matched_by FROM refund_links').fetchone()),('ra','p2','manual'))
        finally:
            case.doCleanups()
