"""Synthetic continuous-use regressions. No personal financial fixtures."""
import csv
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import finance as f


class FinanceV11Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.vault = self.base / 'vault'
        f.init_vault(self.vault)

    def row(self, **updates):
        row = dict(transaction_date='2026-07-01 12:00:00', source_platform='alipay',
                   account_name='示例账户', merchant='示例商店', description='合成商品',
                   amount='100.00', direction='expense', transaction_type='purchase',
                   payment_method='余额', status='交易成功', platform_transaction_id='P1',
                   merchant_order_id='', account_last4='')
        row.update(updates)
        return row

    def source(self, name, rows):
        path = self.base / name
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def context(self, month='2026-07'):
        f.build_all_months(self.vault)
        return json.loads((f.finance_root(self.vault) / 'Data' / ('analysis-context-' + month + '.json')).read_text())

    def count(self, table):
        with f.connect(self.vault) as con:
            return con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def tx(self, platform_id):
        with f.connect(self.vault) as con:
            return con.execute('SELECT * FROM transactions WHERE platform_transaction_id=?', (platform_id,)).fetchone()

    def test_overlapping_exports_count_event_once_keep_all_observations(self):
        week = self.source('week.csv', [self.row()])
        month = self.source('month.csv', [self.row(), self.row(platform_transaction_id='P2', amount='50.00')])
        self.assertEqual(f.import_file(self.vault, week), 1)
        self.assertEqual(f.import_file(self.vault, month), 1)
        self.assertEqual(f.import_file(self.vault, week), 0)
        self.assertEqual(self.context()['metrics']['total_consumption'], '150.00')
        self.assertEqual(self.count('raw_records'), 3)
        self.assertEqual(self.count('transaction_observations'), 3)

    def test_accounts_isolate_ids_and_no_id_purchases_remain_distinct(self):
        path = self.source('accounts.csv', [self.row(), self.row(account_name='另一个账户'),
            self.row(platform_transaction_id=''), self.row(platform_transaction_id='')])
        f.import_file(self.vault, path)
        self.assertEqual(self.context()['metrics']['total_consumption'], '400.00')

    def test_bad_rows_reject_whole_file_and_preserve_archive(self):
        for changes in [dict(amount='-1'), dict(amount='NaN'), dict(amount='Infinity'),
                        dict(transaction_date='2026-02-30'), dict(currency='USD'),
                        dict(direction='sideways'), dict(transaction_type='invented')]:
            with self.subTest(changes=changes):
                path = self.source('bad.csv', [self.row(), self.row(platform_transaction_id='BAD', **changes)])
                with self.assertRaisesRegex(ValueError, '行|row'):
                    f.import_file(self.vault, path)
                self.assertEqual(self.count('transactions'), 0)
        self.assertTrue(list((f.finance_root(self.vault) / 'Raw').rglob('*.csv')))

    def test_extra_source_columns_and_location_are_retained(self):
        row = self.row(source_reference='Raw/example.png', source_locator='image 2, row 3', extra_fact='原始额外列')
        f.import_file(self.vault, self.source('extra.csv', [row]))
        with f.connect(self.vault) as con:
            raw = json.loads(con.execute('SELECT raw_json FROM raw_records').fetchone()[0])
        self.assertEqual(raw['extra_fact'], '原始额外列')
        self.assertEqual(raw['source_locator'], 'image 2, row 3')

    def test_import_receipt_counts_only_current_changes(self):
        first = f.import_with_receipt(self.vault, self.source('one.csv', [self.row()]))
        second = f.import_with_receipt(self.vault, self.source('two.csv', [self.row(), self.row(platform_transaction_id='P2', amount='50')]))
        self.assertEqual((second['inserted_count'], second['duplicate_count']), (1, 1))
        self.assertEqual(second['affected_months'], ['2026-07'])
        self.assertTrue(first['batch_id'])

    def refund(self, **changes):
        return self.row(transaction_date=changes.pop('transaction_date', '2026-08-01 12:00:00'), transaction_type='refund', direction='income',
                        platform_transaction_id=changes.pop('platform_transaction_id', 'R1'), status=changes.pop('status', '退款成功'), **changes)

    def test_manual_choice_survives_later_candidates_and_repeated_matching(self):
        rows = [self.row(), self.row(platform_transaction_id='P2'), self.refund()]
        f.import_file(self.vault, self.source('ambiguous.csv', rows))
        rid, pid = self.tx('R1')['transaction_id'], self.tx('P1')['transaction_id']
        f.link_refund_manually(self.vault, rid, pid)
        f.import_file(self.vault, self.source('later.csv', [self.row(platform_transaction_id='P3')]))
        with f.connect(self.vault) as con:
            for _ in range(3):
                f.match_refunds(con)
            link = con.execute('SELECT * FROM refund_links WHERE refund_id=?', (rid,)).fetchone()
        self.assertEqual((link['purchase_id'], link['matched_by']), (pid, 'manual'))
        f.unlink_refund(self.vault, rid)
        with f.connect(self.vault) as con:
            f.match_refunds(con)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM refund_links').fetchone()[0], 0)

    def test_account_refunds_never_cross_and_duplicate_order_ids_are_ambiguous(self):
        f.import_file(self.vault, self.source('isolated.csv', [self.row(merchant_order_id='shared'),
            self.refund(account_name='另一账户', merchant_order_id='shared')]))
        self.assertEqual(self.count('refund_links'), 0)
        f.import_file(self.vault, self.source('same-order.csv', [self.row(platform_transaction_id='P2', merchant_order_id='shared'), self.refund(platform_transaction_id='R2', merchant_order_id='shared')]))
        self.assertEqual(self.count('refund_links'), 0)

    def test_multiple_refunds_and_refund_first_have_same_result(self):
        purchase = self.row(merchant_order_id='order1')
        r1 = self.refund(amount='30', merchant_order_id='order1')
        r2 = self.refund(platform_transaction_id='R2', amount='70', merchant_order_id='order1', transaction_date='2026-08-02')
        for index, row in enumerate([r2, r1, purchase]):
            f.import_file(self.vault, self.source(str(index)+'.csv', [row]))
        self.assertEqual(self.context()['metrics']['total_consumption'], '0.00')
        with f.connect(self.vault) as con:
            for _ in range(3):
                f.match_refunds(con)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM refund_links').fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM review_items WHERE status='pending' AND issue_type='refund_match'").fetchone()[0], 0)

    def test_failed_unknown_transfer_and_refunded_status_are_not_fabricated_events(self):
        f.import_file(self.vault, self.source('states.csv', [self.row(status='交易关闭'),
            self.row(platform_transaction_id='U', transaction_type='unknown'),
            self.row(platform_transaction_id='T', transaction_type='transfer'),
            self.row(platform_transaction_id='P2', status='已退款')]))
        context = self.context()
        self.assertEqual(context['metrics']['total_consumption'], '100.00')
        self.assertGreaterEqual(context['needs_review_count'], 2)
        self.assertEqual(self.count('refund_links'), 0)
        raw = {'交易时间':'2026-07-01','交易分类':'购物','交易对方':'示例','收/支':'支出','金额':'100','交易状态':'已退款'}
        self.assertEqual(f.normalize_alipay_row(raw)['transaction_type'], 'purchase')

    def test_cross_platform_duplicate_refund_reduces_canonical_purchase(self):
        payment = self.row(payment_method='示例银行信用卡(1234)')
        credit = self.row(source_platform='credit_card', account_name='示例信用卡', account_last4='1234',
                          platform_transaction_id='C1', payment_method='', merchant='支付宝-示例商店')
        refund = self.refund(source_platform='credit_card', account_name='示例信用卡', account_last4='1234',
                             payment_method='', merchant='支付宝-示例商店')
        f.import_file(self.vault, self.source('dual.csv', [payment, credit, refund]))
        self.assertEqual(self.context()['metrics']['total_consumption'], '0.00')

    def test_no_card_evidence_cannot_auto_merge_cross_platform_purchase(self):
        f.import_file(self.vault, self.source('weak.csv', [self.row(),
            self.row(source_platform='credit_card', account_name='未知卡', platform_transaction_id='C1')]))
        self.assertEqual(self.context()['metrics']['total_consumption'], '200.00')

    def test_credit_refund_settlement_debt_only_or_unknown_has_no_cash_inflow(self):
        f.import_file(self.vault, self.source('credit.csv', [self.row(payment_method='花呗'),
            self.refund(payment_method='花呗', settlement='debt_only')]))
        context = self.context('2026-08')
        self.assertEqual(context['metrics']['refund_inflow'], '0.00')
        self.assertEqual(context['metrics']['new_debt'], '0.00')
        f.import_file(self.vault, self.source('unknown-refund.csv', [self.refund(platform_transaction_id='R2', payment_method='花呗', settlement='unknown')]))
        self.assertEqual(self.context('2026-08')['metrics']['refund_inflow'], '0.00')

    def test_reviews_return_stable_ids_candidates_and_snapshot(self):
        f.import_file(self.vault, self.source('review.csv', [self.row(),self.row(platform_transaction_id='P2'),self.refund()]))
        review = next(item for item in f.list_reviews(self.vault) if item['issue_type']=='refund_match')
        self.assertEqual(len(review['candidates']), 2)
        self.assertTrue(review['snapshot'])
        self.assertTrue(review['review_id'])

    def test_category_scopes_and_manual_override_survive_reclassification(self):
        f.import_file(self.vault, self.source('categories.csv', [self.row(),self.row(platform_transaction_id='P2')]))
        p1,p2 = self.tx('P1')['transaction_id'], self.tx('P2')['transaction_id']
        receipt = f.set_category(self.vault,p1,'学习')
        self.assertEqual(self.tx('P1')['category_primary'],'学习')
        self.assertEqual(self.tx('P2')['category_primary'],'待分类')
        f.set_category(self.vault,p2,'购物',scope='merchant-future')
        f.import_file(self.vault,self.source('backfill.csv',[self.row(platform_transaction_id='OLD',transaction_date='2020-01-01')]))
        self.assertEqual(self.tx('OLD')['category_primary'],'待分类')
        f.import_file(self.vault,self.source('future.csv',[self.row(platform_transaction_id='FUTURE',transaction_date='2999-01-01')]))
        self.assertEqual(self.tx('FUTURE')['category_primary'],'购物')
        f.set_category(self.vault,p2,'教育',scope='merchant-all')
        f.reclassify(self.vault)
        self.assertEqual(self.tx('P1')['category_primary'],'学习')
        self.assertEqual(self.tx('P2')['category_primary'],'教育')
        self.assertEqual(self.tx('OLD')['category_primary'],'教育')
        f.undo_decision(self.vault,receipt['decision_id'])
        self.assertEqual(self.tx('P1')['category_primary'],'教育')

    def test_config_reclassify_is_explicit_and_single_overrides_win(self):
        f.import_file(self.vault,self.source('config.csv',[self.row(),self.row(platform_transaction_id='P2')]))
        f.set_category(self.vault,self.tx('P1')['transaction_id'],'单笔')
        path=f.finance_root(self.vault)/'Data/config.json'
        config=json.loads(path.read_text());config['merchant_overrides']['示例商店']='历史新类'
        path.write_text(json.dumps(config))
        f.reclassify(self.vault)
        self.assertEqual(self.tx('P2')['category_primary'],'历史新类')
        self.assertEqual(self.tx('P1')['category_primary'],'单笔')

    def test_undo_overlapping_batches_preserves_other_support_and_raw_history(self):
        one=f.import_with_receipt(self.vault,self.source('one.csv',[self.row()]))
        two=f.import_with_receipt(self.vault,self.source('two.csv',[self.row(),self.row(platform_transaction_id='P2',amount='50')]))
        f.undo_import(self.vault,one['batch_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'150.00')
        f.undo_import(self.vault,two['batch_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        again=f.undo_import(self.vault,two['batch_id'])
        self.assertEqual(again['status'],'already_undone')
        self.assertEqual(self.count('raw_records'),3)
        self.assertEqual(self.count('transactions'),2)
        self.assertGreater(self.count('decisions'),0)
        with self.assertRaisesRegex(ValueError,'撤销'):
            f.import_file(self.vault,self.base/'one.csv')
        f.import_with_receipt(self.vault,self.base/'one.csv',reactivate=True)
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')

    def test_undo_refund_restores_purchase_and_receipt_explains_original_month(self):
        f.import_file(self.vault,self.source('purchase.csv',[self.row()]))
        refund=f.import_with_receipt(self.vault,self.source('refund.csv',[self.refund()]))
        self.assertEqual(refund['consumption_changes']['2026-07']['delta'],'-100.00')
        self.assertIn('2026-07',refund['affected_months'])
        undone=f.undo_import(self.vault,refund['batch_id'])
        self.assertEqual(undone['consumption_changes']['2026-07']['delta'],'100.00')
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')

    def test_stale_review_snapshot_is_rejected(self):
        f.import_file(self.vault,self.source('a.csv',[self.row(),self.row(platform_transaction_id='P2'),self.refund()]))
        review=next(r for r in f.list_reviews(self.vault) if r['issue_type']=='refund_match')
        f.import_file(self.vault,self.source('b.csv',[self.row(platform_transaction_id='P3')]))
        with self.assertRaisesRegex(ValueError,'变化'):
            f.link_refund_manually(self.vault,review['transaction_id'],self.tx('P1')['transaction_id'],snapshot=review['snapshot'])

    def test_user_notes_and_custom_properties_survive_month_and_dashboard_rebuild(self):
        f.import_file(self.vault,self.source('notes.csv',[self.row()]))
        f.build_all_months(self.vault)
        for relative in ['Months/2026-07.md','Dashboard.md']:
            path=f.finance_root(self.vault)/relative
            original=path.read_text()
            path.write_text(original.replace('---\n','---\nmy_property: 我自己写的\n',1)+'\n手写复盘：逐字保留 | #备注\n')
        f.build_all_months(self.vault)
        for relative in ['Months/2026-07.md','Dashboard.md']:
            text=(f.finance_root(self.vault)/relative).read_text()
            self.assertIn('my_property: 我自己写的',text)
            self.assertTrue(text.endswith('\n手写复盘：逐字保留 | #备注\n'))

    def test_analysis_fingerprint_stable_for_repeat_but_invalidated_by_cross_month_refund(self):
        source=self.source('analysis.csv',[self.row()])
        f.import_file(self.vault,source)
        ctx=self.context()
        f.write_analysis(self.vault,'2026-07',ctx['input_fingerprint'],'示例商店消费100元。','复核一项。')
        f.import_file(self.vault,source)
        unchanged=self.context()
        self.assertEqual(unchanged['input_fingerprint'],ctx['input_fingerprint'])
        self.assertEqual(unchanged['analysis_status'],'current')
        self.assertIn('示例商店消费100元。',(f.finance_root(self.vault)/'Months/2026-07.md').read_text())
        f.import_file(self.vault,self.source('late.csv',[self.refund()]))
        changed=self.context()
        self.assertEqual(changed['analysis_status'],'stale')
        self.assertNotIn('示例商店消费100元。',(f.finance_root(self.vault)/'Months/2026-07.md').read_text())
        with self.assertRaisesRegex(ValueError,'过期|变化'):
            f.write_analysis(self.vault,'2026-07',ctx['input_fingerprint'],'旧结论','旧建议')
        self.assertTrue(list((f.finance_root(self.vault)/'Backups').rglob('*analysis*')))

    def test_legacy_page_backed_up_and_unknown_text_preserved(self):
        f.import_file(self.vault,self.source('legacy.csv',[self.row()]))
        page=f.finance_root(self.vault)/'Months/2026-07.md'
        legacy='---\nmy_tag: example\n---\n# 我的旧月报\n自己写的未知格式\n'
        page.write_text(legacy)
        f.build_all_months(self.vault)
        self.assertIn('自己写的未知格式',page.read_text())
        self.assertTrue(any(p.read_text()==legacy for p in (f.finance_root(self.vault)/'Backups').glob('*.md')))

    def test_broken_managed_markers_stop_overwrite_and_report_pending_state(self):
        f.import_file(self.vault,self.source('markers.csv',[self.row()]))
        f.build_all_months(self.vault)
        page=f.finance_root(self.vault)/'Months/2026-07.md'
        broken=page.read_text().replace('<!-- FINANCE_AUTO_END -->','')
        page.write_text(broken)
        with self.assertRaisesRegex(ValueError,'标记'):
            f.build_all_months(self.vault)
        self.assertEqual(page.read_text(),broken)
        with f.connect(self.vault) as con:
            self.assertEqual(con.execute('SELECT status FROM report_state').fetchone()[0],'failed')

    def test_empty_and_debt_only_dashboard_use_unknown_not_zero(self):
        f.build_all_months(self.vault)
        dashboard=f.finance_root(self.vault)/'Dashboard.md'
        self.assertTrue(dashboard.exists())
        self.assertIn('尚未导入账单',dashboard.read_text())
        self.assertIn('未知',dashboard.read_text())
        f.save_debt_snapshot(self.vault,'示例白条','2026-07-31','5000','','')
        f.build_all_months(self.vault)
        text=dashboard.read_text()
        self.assertIn('5,000.00',text)
        self.assertIn('2026-07-31',text)
        self.assertIn('未知',text)

    def test_coverage_propagates_and_review_amount_not_double_counted(self):
        rows=[self.row(transaction_type='unknown'),self.row(platform_transaction_id='T',transaction_type='transfer')]
        f.import_with_receipt(self.vault,self.source('partial.csv',rows),coverage='partial',source_reference='image-1')
        context=self.context()
        self.assertEqual(context['coverage']['status'],'partial')
        self.assertEqual(context['reconciliation_status'],'unreconciled')
        self.assertEqual(context['review_amount'],'200.00')
        self.assertEqual(context['needs_review_count'],2)
        for relative in ['Months/2026-07.md','Dashboard.md']:
            self.assertIn('部分',(f.finance_root(self.vault)/relative).read_text())
            self.assertNotIn('已对平',(f.finance_root(self.vault)/relative).read_text())

    def test_full_refunds_excluded_from_categories_counts_and_habits(self):
        f.import_file(self.vault,self.source('full.csv',[self.row(),self.refund()]))
        ctx=self.context()
        self.assertEqual(ctx['categories'],[])
        self.assertEqual(ctx['top_merchants'],[])
        self.assertEqual(ctx['purchase_count'],0)
        refunds=(f.finance_root(self.vault)/'Reviews/退款核对.md').read_text()
        self.assertIn('示例商店',refunds)
        self.assertIn('Raw/',refunds)

    def test_dashboard_uses_latest_data_and_no_trend_when_coverage_incomparable(self):
        f.import_with_receipt(self.vault,self.source('june.csv',[self.row(transaction_date='2026-06-01',source_platform='wechat')]),coverage='partial')
        f.import_with_receipt(self.vault,self.source('july.csv',[self.row()]),coverage='complete')
        ctx=self.context()
        text=(f.finance_root(self.vault)/'Dashboard.md').read_text()
        self.assertIn('最新数据：2026-07',text)
        self.assertIn('不可比',text)
        self.assertFalse(ctx['comparison']['comparable'])
        self.assertEqual(sum(int(float(c['amount'])*100) for c in ctx['categories']),10000)

    def test_later_credit_refund_does_not_rewrite_historical_new_debt(self):
        f.import_file(self.vault,self.source('loan.csv',[self.row(amount='6000',payment_method='花呗'),
            self.refund(amount='6000',payment_method='花呗',settlement='debt_only')]))
        ctx=self.context()
        self.assertEqual(ctx['metrics']['total_consumption'],'0.00')
        self.assertEqual(ctx['metrics']['new_debt'],'6000.00')

    def test_manual_type_and_settlement_review_are_reversible_and_prioritized(self):
        f.import_file(self.vault,self.source('unclear.csv',[self.row(transaction_type='unknown')]))
        item=next(r for r in f.list_reviews(self.vault) if r['issue_type']=='type_unknown')
        receipt=f.resolve_review(self.vault,item['review_id'],item['snapshot'],{'transaction_type':'purchase'})
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')
        f.undo_decision(self.vault,receipt['decision_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        self.assertTrue(any(r['issue_type']=='type_unknown' for r in f.list_reviews(self.vault)))

    def test_manual_duplicate_decision_is_not_reset_by_automatic_rebuild(self):
        f.import_file(self.vault,self.source('duplicates.csv',[self.row(platform_transaction_id=''),self.row(platform_transaction_id='')]))
        item=next(r for r in f.list_reviews(self.vault) if r['issue_type']=='duplicate_candidate')
        target=item['candidates'][0]['transaction_id']
        result=f.resolve_review(self.vault,item['review_id'],item['snapshot'],{'duplicate_of':target})
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')
        f.undo_decision(self.vault,result['decision_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'200.00')

    def test_invalid_native_date_is_not_silently_dropped(self):
        path=self.base/'native.csv'
        path.write_text('交易时间,交易分类,交易对方,收/支,金额,交易状态\n2026-07-01,购物,示例,支出,100,成功\nnot-a-date,购物,示例,支出,50,成功\n')
        with self.assertRaisesRegex(ValueError,'行|日期'):
            f.import_file(self.vault,path)
        self.assertEqual(self.count('transactions'),0)

    def test_invalid_config_is_clear_error_not_partial_import(self):
        path=f.finance_root(self.vault)/'Data/config.json'
        source=self.source('invalid-config.csv',[self.row()])
        for config in ([],{'category_rules':[{'pattern':'[','category_primary':'坏规则'}]}):
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError,'config|规则'):
                f.import_file(self.vault,source)
            self.assertEqual(self.count('transactions'),0)

    def test_comparison_inputs_invalidate_following_month_analysis(self):
        f.import_with_receipt(self.vault,self.source('june-complete.csv',[self.row(transaction_date='2026-06-01')]),coverage='complete')
        f.import_with_receipt(self.vault,self.source('july-complete.csv',[self.row(platform_transaction_id='P2')]),coverage='complete')
        july=self.context()
        self.assertTrue(july['comparison']['comparable'])
        f.write_analysis(self.vault,'2026-07',july['input_fingerprint'],'两月金额均100元。','保留复核。')
        f.import_with_receipt(self.vault,self.source('june-late.csv',[self.row(transaction_date='2026-06-02',platform_transaction_id='P3')]),coverage='complete')
        with self.assertRaisesRegex(ValueError,'过期|变化'):
            f.write_analysis(self.vault,'2026-07',july['input_fingerprint'],'旧比较','旧建议')
        self.assertEqual(self.context()['analysis_status'],'stale')

    def test_same_id_conflict_has_visible_uncertainty_and_never_silently_counts_first_row(self):
        f.import_file(self.vault,self.source('conflict-a.csv',[self.row()]))
        f.import_file(self.vault,self.source('conflict-b.csv',[self.row(amount='50')]))
        self.assertTrue(any(r['issue_type']=='source_conflict' for r in f.list_reviews(self.vault)))
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')

    def test_single_category_receipt_reports_the_actual_update(self):
        f.import_file(self.vault,self.source('category-receipt.csv',[self.row()]))
        receipt=f.set_category(self.vault,self.tx('P1')['transaction_id'],'学习')
        self.assertEqual(receipt['updated_count'],1)
        self.assertEqual(receipt['affected_months'],['2026-07'])

    def test_import_receipt_and_index_identify_original_file_for_future_undo(self):
        receipt=f.import_with_receipt(self.vault,self.source('weekly-example.csv',[self.row()]))
        self.assertEqual(receipt['source_name'],'weekly-example.csv')
        self.assertTrue(receipt['created_at'])
        f.build_all_months(self.vault)
        index=(f.finance_root(self.vault)/'Imports/导入记录.md').read_text()
        self.assertIn('weekly-example.csv',index)

    def test_undo_conflicting_observation_restores_remaining_supported_fact(self):
        f.import_file(self.vault,self.source('good-source.csv',[self.row()]))
        conflict=f.import_with_receipt(self.vault,self.source('conflict-source.csv',[self.row(amount='200')]))
        f.undo_import(self.vault,conflict['batch_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')
        self.assertFalse(any(r['issue_type']=='source_conflict' for r in f.list_reviews(self.vault)))

    def test_conflicting_status_observations_are_order_independent_and_reviewed(self):
        closed=self.row(status='交易关闭');success=self.row(status='交易成功')
        for name,row in [('closed',closed),('success',success)]:
            f.import_file(self.vault,self.source(name+'.csv',[row]))
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        self.assertTrue(any(r['issue_type']=='source_conflict' for r in f.list_reviews(self.vault)))
        reverse=self.base/'reverse';f.init_vault(reverse)
        for name in ('success','closed'):
            f.import_file(reverse,self.base/(name+'.csv'))
        f.build_all_months(reverse)
        ctx=json.loads((reverse/'Finance/Data/analysis-context-2026-07.json').read_text())
        self.assertEqual(ctx['metrics']['total_consumption'],'0.00')

    def test_pending_purchase_and_refund_cannot_count_as_successful_events(self):
        f.import_file(self.vault,self.source('pending-purchase.csv',[self.row(status='等待付款')]))
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        self.assertTrue(any(r['issue_type']=='status_unknown' for r in f.list_reviews(self.vault)))
        f.import_file(self.vault,self.source('success-purchase.csv',[self.row(platform_transaction_id='P2')]))
        f.import_file(self.vault,self.source('pending-refund.csv',[self.refund(status='退款处理中')]))
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')
        self.assertEqual(self.count('refund_links'),0)

    def test_unknown_purchase_settlement_does_not_claim_confirmed_cash(self):
        f.import_file(self.vault,self.source('cash-unknown.csv',[self.row(payment_method='',settlement='unknown')]))
        ctx=self.context()
        self.assertEqual(ctx['metrics']['cash_outflow'],'0.00')
        self.assertGreater(ctx['settlement_unknown_count'],0)
        self.assertTrue(any(r['issue_type']=='settlement_unknown' for r in f.list_reviews(self.vault)))

    def test_debt_snapshot_marks_existing_pages_pending(self):
        f.build_all_months(self.vault)
        f.save_debt_snapshot(self.vault,'示例信用卡','2026-07-31','100','','')
        self.assertEqual(f.read_report(self.vault)['artifact_status'],'pending')

    def test_source_markdown_is_inert_text_in_month_and_dashboard(self):
        malicious='![tracking](https://example.invalid/x.png) ![[secret]] `code`'
        f.import_file(self.vault,self.source('inert.csv',[self.row(merchant=malicious)]))
        self.context()
        for relative in ('Months/2026-07.md','Dashboard.md'):
            text=(f.finance_root(self.vault)/relative).read_text()
            self.assertNotIn('![tracking]',text)
            self.assertNotIn('![[secret]]',text)

    def test_source_conflict_can_be_chosen_reversed_and_disabled_with_its_batch(self):
        a=f.import_with_receipt(self.vault,self.source('source-a.csv',[self.row()]))
        b=f.import_with_receipt(self.vault,self.source('source-b.csv',[self.row(amount='200')]))
        item=next(r for r in f.list_reviews(self.vault) if r['issue_type']=='source_conflict')
        chosen=next(s['raw_id'] for s in item['sources'] if 'source-b.csv' in s['source_file'])
        decision=f.resolve_review(self.vault,item['review_id'],item['snapshot'],{'raw_id':chosen})
        self.assertEqual(self.context()['metrics']['total_consumption'],'200.00')
        f.undo_decision(self.vault,decision['decision_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        item=next(r for r in f.list_reviews(self.vault) if r['issue_type']=='source_conflict')
        f.resolve_review(self.vault,item['review_id'],item['snapshot'],{'raw_id':chosen})
        f.undo_import(self.vault,b['batch_id'])
        self.assertEqual(self.context()['metrics']['total_consumption'],'0.00')
        self.assertTrue(any(r['issue_type']=='manual_inactive' for r in f.list_reviews(self.vault)))

    def test_processing_refund_does_not_reduce_successful_purchase(self):
        f.import_file(self.vault,self.source('success.csv',[self.row()]))
        f.import_file(self.vault,self.source('processing.csv',[self.refund(status='退款处理中')]))
        self.assertEqual(self.context()['metrics']['total_consumption'],'100.00')
        self.assertEqual(self.count('refund_links'),0)

    def test_native_row_without_date_rejects_file_instead_of_disappearing(self):
        path=self.base/'missing-date.csv'
        path.write_text('交易时间,交易分类,交易对方,收/支,金额,交易状态\n2026-07-01,购物,示例,支出,100,成功\n,购物,示例,支出,50,成功\n')
        with self.assertRaisesRegex(ValueError,'行|日期'):
            f.import_file(self.vault,path)
        self.assertEqual(self.count('transactions'),0)

    def test_native_income_and_yield_are_not_purchases(self):
        base={'交易时间':'2026-07-01','交易分类':'收入','交易对方':'示例','收/支':'收入','金额':'100','交易状态':'交易成功'}
        self.assertEqual(f.normalize_alipay_row(base)['transaction_type'],'income')
        base['商品说明']='余额宝收益'
        self.assertEqual(f.normalize_alipay_row(base)['transaction_type'],'investment_income')

    def test_receipt_refund_count_is_current_operation_not_historical_total(self):
        f.import_file(self.vault,self.source('past-refund.csv',[self.row(),self.refund()]))
        receipt=f.import_with_receipt(self.vault,self.source('unrelated.csv',[self.row(platform_transaction_id='P2',merchant='另一个示例商户')]))
        self.assertEqual(receipt['linked_refund_count'],0)
        self.assertEqual(receipt['total_linked_refund_count'],1)

    def test_dashboard_lists_older_pending_items_and_current_analysis_summary(self):
        f.import_file(self.vault,self.source('old-review.csv',[self.row(transaction_date='2026-06-01',transaction_type='unknown')]))
        f.import_file(self.vault,self.source('new-data.csv',[self.row(platform_transaction_id='P2')]))
        ctx=self.context()
        f.write_analysis(self.vault,'2026-07',ctx['input_fingerprint'],'本期示例商店净消费100元。','核对一项记录。')
        dashboard=(f.finance_root(self.vault)/'Dashboard.md').read_text()
        self.assertIn('全部月份待确认：1',dashboard)
        self.assertIn('本期示例商店净消费100元。',dashboard)
        self.assertIn('alipay/示例账户',dashboard)
        self.assertIn('2026-07-01',dashboard)

    def test_explicit_native_account_scope_isolated_across_same_id_exports(self):
        path=self.base/'native-account.csv'
        path.write_text('交易时间,交易分类,交易对方,收/支,金额,交易状态,交易订单号\n2026-07-01,购物,示例,支出,100,成功,NATIVE1\n')
        a=f.import_with_receipt(self.vault,path,account_name='示例账户甲')
        b=f.import_with_receipt(self.vault,path,account_name='示例账户乙')
        self.assertEqual(self.context()['metrics']['total_consumption'],'200.00')
        self.assertNotEqual(a['batch_id'],b['batch_id'])

    def test_extracted_row_links_to_archived_image_and_visible_location(self):
        image=self.base/'synthetic.png';image.write_bytes(b'synthetic image placeholder')
        original=f.archive_file(self.vault,image,'screenshot','partial')
        relative=original.relative_to(f.finance_root(self.vault)).as_posix()
        f.import_with_receipt(self.vault,self.source('extracted.csv',[self.row(source_reference=relative,source_locator='image 1 row 2')]),coverage='partial')
        self.context()
        text=(f.finance_root(self.vault)/'Months/2026-07.md').read_text()
        self.assertIn(original.name,text)
        self.assertIn('image 1 row 2',text)
