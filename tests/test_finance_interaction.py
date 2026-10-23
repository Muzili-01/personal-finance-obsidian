"""CLI behavior checks against synthetic Vaults only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import finance as f
CLI=Path(f.__file__)


def files(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=tempfile.gettempdir());self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)

    def cli(self,*args,ok=True):
        result=subprocess.run([sys.executable,'-B',str(CLI),*map(str,args)],capture_output=True,text=True)
        if ok:
            self.assertEqual(result.returncode,0,result.stderr)
        else:
            self.assertNotEqual(result.returncode,0)
        return result

    def test_doctor_checks_missing_and_existing_vault_without_writes(self):
        missing=self.base/'missing'
        result=self.cli('doctor','--vault',missing)
        self.assertFalse(missing.exists())
        self.assertFalse(json.loads(result.stdout)['exists'])
        existing=self.base/'vault';f.init_vault(existing)
        before=files(existing)
        self.cli('doctor','--vault',existing)
        self.assertEqual(files(existing),before)

    def test_demo_only_creates_new_target_and_read_report_has_no_side_effects(self):
        vault=self.base/'demo'
        self.cli('demo','--vault',vault)
        self.assertTrue((vault/'Finance/Dashboard.md').exists())
        before=files(vault)
        self.cli('demo','--vault',vault,ok=False)
        self.assertEqual(files(vault),before)
        result=self.cli('report','--vault',vault)
        self.assertIn('Dashboard',result.stdout)
        self.assertEqual(files(vault),before)
        self.cli('review','--vault',vault,'--json')
        self.assertEqual(files(vault),before)

    def test_cli_commands_perform_category_undo_and_show_json_receipts(self):
        vault=self.base/'demo';self.cli('demo','--vault',vault)
        with f.connect(vault) as con:
            tid=con.execute("SELECT transaction_id FROM transactions WHERE transaction_type='purchase' LIMIT 1").fetchone()[0]
            batch=con.execute('SELECT batch_id FROM import_batches LIMIT 1').fetchone()[0]
        receipt=json.loads(self.cli('categorize','--vault',vault,'--transaction-id',tid,'--category','学习','--json').stdout)
        self.assertTrue(receipt['decision_id'])
        self.cli('undo-decision','--vault',vault,'--decision-id',receipt['decision_id'])
        undone=json.loads(self.cli('undo-import','--vault',vault,'--batch-id',batch).stdout)
        self.assertEqual(undone['status'],'undone')
        self.assertEqual(json.loads(self.cli('report','--vault',vault,'--json').stdout)['artifact_status'],'pending')
        help_text=self.cli('--help').stdout
        for command in ('doctor','demo','report','review','categorize','undo-import','write-analysis'):
            self.assertIn(command,help_text)

    def test_review_missing_database_does_not_create_database(self):
        vault=self.base/'missing-db';(vault/'Finance/Data').mkdir(parents=True)
        before=files(vault)
        self.cli('review','--vault',vault,ok=False)
        self.assertEqual(files(vault),before)

    def test_legacy_public_report_functions_remain_callable(self):
        vault=self.base/'demo';f.demo(vault)
        f.build_review_and_quality_pages(vault)
        context=f.write_analysis_context(vault,'2026-07',[],0,0,0,0,0,0)
        self.assertTrue(context.exists())
        dashboard=f.build_dashboard(vault,'2026-07',0,0,0,0,0)
        self.assertTrue(dashboard.exists())

    def test_parallel_writer_is_refused_without_modifying_ledger(self):
        from finance_storage import vault_lock
        vault=self.base/'demo';f.demo(vault)
        with vault_lock(f.finance_root(vault)):
            before=files(vault)
            result=self.cli('build-all','--vault',vault,ok=False)
            self.assertIn('正在写入',result.stderr)
            self.assertEqual(files(vault),before)
