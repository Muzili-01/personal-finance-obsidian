from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLI = SKILL_ROOT / "scripts" / "finance.py"


class FinanceCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def write_normalized(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames = [
            "transaction_date",
            "source_platform",
            "account_name",
            "merchant",
            "description",
            "amount",
            "direction",
            "transaction_type",
            "payment_method",
            "status",
            "platform_transaction_id",
            "merchant_order_id",
            "account_last4",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def test_full_refund_disappears_from_consumption_report_but_raw_rows_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-10 09:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "星巴克",
                        "description": "咖啡",
                        "amount": "45.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "零钱",
                        "status": "支付成功",
                        "platform_transaction_id": "W100",
                        "merchant_order_id": "M100",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-12 10:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "星巴克",
                        "description": "退款-咖啡",
                        "amount": "45.00",
                        "direction": "income",
                        "transaction_type": "refund",
                        "payment_method": "零钱",
                        "status": "退款成功",
                        "platform_transaction_id": "W900",
                        "merchant_order_id": "M100",
                        "account_last4": "",
                    },
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 0.00", report)
            visible_section = report.split("## 消费明细", 1)[1].split("##", 1)[0]
            self.assertNotIn("星巴克", visible_section)

            database = vault / "Finance" / "Data" / "finance.sqlite3"
            with sqlite3.connect(database) as connection:
                raw_count = connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
                transaction_count = connection.execute(
                    "SELECT COUNT(*) FROM transactions"
                ).fetchone()[0]
            self.assertEqual(raw_count, 2)
            self.assertEqual(transaction_count, 2)

    def test_unique_refund_without_order_id_is_linked_by_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-10 09:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "星巴克咖啡",
                        "description": "饮品",
                        "amount": "45.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "零钱",
                        "status": "支付成功",
                        "platform_transaction_id": "W100",
                        "merchant_order_id": "",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-12 10:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "星巴克咖啡",
                        "description": "退款",
                        "amount": "45.00",
                        "direction": "income",
                        "transaction_type": "refund",
                        "payment_method": "零钱",
                        "status": "退款成功",
                        "platform_transaction_id": "W900",
                        "merchant_order_id": "",
                        "account_last4": "",
                    },
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 0.00", report)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                link = connection.execute(
                    "SELECT matched_by, confidence FROM refund_links"
                ).fetchone()
            self.assertEqual(link[0], "unique_merchant_amount")
            self.assertGreaterEqual(link[1], 0.90)

    def test_ambiguous_refund_stays_visible_in_review_without_hiding_a_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            rows = []
            for day, transaction_id in (("15", "A200"), ("16", "A201")):
                rows.append(
                    {
                        "transaction_date": "2026-07-{0} 12:00:00".format(day),
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "美团外卖",
                        "description": "午餐",
                        "amount": "36.50",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": transaction_id,
                        "merchant_order_id": "",
                        "account_last4": "",
                    }
                )
            rows.append(
                {
                    "transaction_date": "2026-07-17 09:00:00",
                    "source_platform": "alipay",
                    "account_name": "支付宝",
                    "merchant": "美团外卖",
                    "description": "退款",
                    "amount": "36.50",
                    "direction": "income",
                    "transaction_type": "refund",
                    "payment_method": "余额",
                    "status": "退款成功",
                    "platform_transaction_id": "A900",
                    "merchant_order_id": "",
                    "account_last4": "",
                }
            )
            self.write_normalized(source, rows)

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 73.00", report)
            self.assertIn("needs_review_count: 1", report)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                link_count = connection.execute("SELECT COUNT(*) FROM refund_links").fetchone()[0]
                review_count = connection.execute("SELECT COUNT(*) FROM review_items").fetchone()[0]
            self.assertEqual(link_count, 0)
            self.assertEqual(review_count, 1)

    def test_payment_platform_and_credit_card_views_of_one_purchase_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-02 20:00:00",
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "京东数码",
                        "description": "数据线",
                        "amount": "299.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "建设银行信用卡(1234)",
                        "status": "交易成功",
                        "platform_transaction_id": "A100",
                        "merchant_order_id": "J100",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-03 00:00:00",
                        "source_platform": "credit_card",
                        "account_name": "建设银行信用卡",
                        "merchant": "支付宝-京东数码",
                        "description": "支付宝-京东数码",
                        "amount": "299.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "",
                        "status": "已记账",
                        "platform_transaction_id": "",
                        "merchant_order_id": "",
                        "account_last4": "1234",
                    },
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 299.00", report)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                duplicate = connection.execute(
                    "SELECT excluded_reason, duplicate_of FROM transactions WHERE source_platform = 'credit_card'"
                ).fetchone()
            self.assertEqual(duplicate[0], "duplicate")
            self.assertTrue(duplicate[1])

    def test_alipay_gb18030_export_is_detected_after_intro_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "alipay.csv"
            source.write_text(
                "\n".join(
                    [
                        "支付宝交易记录明细查询",
                        "账号：[已脱敏]",
                        "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收付款方式,交易状态,交易订单号,商家订单号,备注",
                        "2026-07-10 09:00:00,消费,星巴克,,咖啡,支出,45.00,余额,交易成功,A100,M100,",
                        "2026-07-12 10:00:00,退款,星巴克,,退款-咖啡,不计收支,45.00,余额,退款成功,A900,M100,",
                        "--------------------------",
                        "共2笔记录",
                    ]
                ),
                encoding="gb18030",
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 0.00", report)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                values = connection.execute(
                    "SELECT source_platform, transaction_type FROM transactions ORDER BY transaction_time"
                ).fetchall()
            self.assertEqual(values, [("alipay", "purchase"), ("alipay", "refund")])

    def test_wechat_export_classifies_refunds_transfers_and_repayments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "wechat.csv"
            source.write_text(
                "\n".join(
                    [
                        "微信支付账单明细",
                        "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注",
                        "2026-07-10 09:00:00,商户消费,星巴克,咖啡,支出,¥45.00,零钱,支付成功,W100,M100,",
                        "2026-07-12 10:00:00,商户消费-退款,星巴克,咖啡退款,收入,¥45.00,零钱,已退款,W900,M100,",
                        "2026-07-14 12:00:00,零钱通转出,零钱通,转出到零钱,/,¥1000.00,零钱通,已到账,W300,,",
                        "2026-07-15 12:00:00,信用卡还款,建设银行信用卡,还款,/,¥3000.00,零钱,还款成功,W400,,",
                    ]
                ),
                encoding="utf-8-sig",
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 0.00", report)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                types = [
                    row[0]
                    for row in connection.execute(
                        "SELECT transaction_type FROM transactions ORDER BY transaction_time"
                    ).fetchall()
                ]
            self.assertEqual(types, ["purchase", "refund", "transfer", "repayment"])

    def test_import_is_idempotent_and_archives_the_immutable_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-01 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "便利店",
                        "description": "饮料",
                        "amount": "8.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": "A1",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    }
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))

            archived = list((vault / "Finance" / "Raw" / "alipay").glob("*-transactions.csv"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), source.read_bytes())
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                transaction_count = connection.execute(
                    "SELECT COUNT(*) FROM transactions"
                ).fetchone()[0]
                stored_source = connection.execute(
                    "SELECT source_file FROM raw_records"
                ).fetchone()[0]
            self.assertEqual(transaction_count, 1)
            self.assertEqual(Path(stored_source).resolve(), archived[0].resolve())

    def test_debt_snapshot_is_written_to_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            self.run_cli("init", "--vault", str(vault))
            self.run_cli(
                "debt",
                "--vault",
                str(vault),
                "--account",
                "京东白条",
                "--as-of",
                "2026-07-31",
                "--outstanding",
                "5000.00",
                "--due",
                "1000.00",
                "--due-date",
                "2026-08-10",
            )
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            dashboard = (vault / "Finance" / "Dashboard.md").read_text(encoding="utf-8")
            self.assertIn("京东白条", dashboard)
            self.assertIn("¥5,000.00", dashboard)
            self.assertIn("2026-08-10", dashboard)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                count = connection.execute("SELECT COUNT(*) FROM debt_snapshots").fetchone()[0]
            self.assertEqual(count, 1)

    def test_consumption_and_cashflow_metrics_use_separate_accounting_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-01 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "超市",
                        "description": "日用品",
                        "amount": "100.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": "A1",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-02 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "超市",
                        "description": "部分退款",
                        "amount": "20.00",
                        "direction": "income",
                        "transaction_type": "refund",
                        "payment_method": "余额",
                        "status": "退款成功",
                        "platform_transaction_id": "A2",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-20 12:00:00",
                        "source_platform": "bank",
                        "account_name": "储蓄卡",
                        "merchant": "京东白条",
                        "description": "分期本金还款",
                        "amount": "1000.00",
                        "direction": "expense",
                        "transaction_type": "repayment",
                        "payment_method": "储蓄卡",
                        "status": "成功",
                        "platform_transaction_id": "B1",
                        "merchant_order_id": "",
                        "account_last4": "5678",
                    },
                    {
                        "transaction_date": "2026-07-21 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "余额宝",
                        "merchant": "本人银行卡",
                        "description": "余额宝转出",
                        "amount": "500.00",
                        "direction": "neutral",
                        "transaction_type": "transfer",
                        "payment_method": "余额宝",
                        "status": "成功",
                        "platform_transaction_id": "A3",
                        "merchant_order_id": "",
                        "account_last4": "",
                    },
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 80.00", report)
            self.assertIn("cash_outflow: 1100.00", report)
            self.assertIn("refund_inflow: 20.00", report)
            self.assertIn("debt_repayment: 1000.00", report)

    def test_ambiguous_refund_can_be_resolved_without_editing_raw_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            rows = []
            for day, transaction_id in (("15", "A200"), ("16", "A201")):
                rows.append(
                    {
                        "transaction_date": "2026-07-{0} 12:00:00".format(day),
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "美团外卖",
                        "description": "午餐",
                        "amount": "36.50",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": transaction_id,
                        "merchant_order_id": "",
                        "account_last4": "",
                    }
                )
            rows.append(
                {
                    "transaction_date": "2026-07-17 09:00:00",
                    "source_platform": "alipay",
                    "account_name": "支付宝",
                    "merchant": "美团外卖",
                    "description": "退款",
                    "amount": "36.50",
                    "direction": "income",
                    "transaction_type": "refund",
                    "payment_method": "余额",
                    "status": "退款成功",
                    "platform_transaction_id": "A900",
                    "merchant_order_id": "",
                    "account_last4": "",
                }
            )
            self.write_normalized(source, rows)
            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))

            database = vault / "Finance" / "Data" / "finance.sqlite3"
            with sqlite3.connect(database) as connection:
                purchase_id = connection.execute(
                    "SELECT transaction_id FROM transactions WHERE platform_transaction_id = 'A200'"
                ).fetchone()[0]
                refund_id = connection.execute(
                    "SELECT transaction_id FROM transactions WHERE platform_transaction_id = 'A900'"
                ).fetchone()[0]
                raw_before = connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]

            self.run_cli(
                "link-refund",
                "--vault",
                str(vault),
                "--refund-id",
                refund_id,
                "--purchase-id",
                purchase_id,
            )
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 36.50", report)
            self.assertIn("needs_review_count: 0", report)
            with sqlite3.connect(database) as connection:
                raw_after = connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
            self.assertEqual(raw_before, raw_after)

    def test_build_writes_review_quality_and_analysis_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            rows = []
            for day, transaction_id in (("15", "A200"), ("16", "A201")):
                rows.append(
                    {
                        "transaction_date": "2026-07-{0} 12:00:00".format(day),
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "美团外卖",
                        "description": "午餐",
                        "amount": "36.50",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": transaction_id,
                        "merchant_order_id": "",
                        "account_last4": "",
                    }
                )
            rows.append(
                {
                    "transaction_date": "2026-07-17 09:00:00",
                    "source_platform": "alipay",
                    "account_name": "支付宝",
                    "merchant": "美团外卖",
                    "description": "退款",
                    "amount": "36.50",
                    "direction": "income",
                    "transaction_type": "refund",
                    "payment_method": "余额",
                    "status": "退款成功",
                    "platform_transaction_id": "A900",
                    "merchant_order_id": "",
                    "account_last4": "",
                }
            )
            self.write_normalized(source, rows)
            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            finance = vault / "Finance"
            review_path = finance / "Reviews" / "待确认交易.md"
            quality_path = finance / "Reviews" / "数据质量报告.md"
            context_path = finance / "Data" / "analysis-context-2026-07.json"
            self.assertTrue(review_path.exists())
            self.assertTrue(quality_path.exists())
            self.assertTrue(context_path.exists())
            review = review_path.read_text(encoding="utf-8")
            quality = quality_path.read_text(encoding="utf-8")
            report = (finance / "Months" / "2026-07.md").read_text(encoding="utf-8")
            context = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertIn("美团外卖", review)
            self.assertIn("候选消费", review)
            self.assertIn("待确认交易：1", quality)
            self.assertIn("## 消费习惯分析", report)
            self.assertIn("## 下月建议", report)
            self.assertIn("<!-- AI_SUGGESTIONS_END -->\n\n## 数据质量", report)
            self.assertEqual(context["month"], "2026-07")
            self.assertEqual(context["needs_review_count"], 1)

    def test_rebuild_preserves_agent_written_analysis_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-01 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "支付宝",
                        "merchant": "便利店",
                        "description": "早餐",
                        "amount": "18.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "余额",
                        "status": "交易成功",
                        "platform_transaction_id": "A1",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    }
                ],
            )
            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")

            report_path = vault / "Finance" / "Months" / "2026-07.md"
            report = report_path.read_text(encoding="utf-8")
            start = "<!-- AI_ANALYSIS_START -->"
            end = "<!-- AI_ANALYSIS_END -->"
            before, remainder = report.split(start, 1)
            _, after = remainder.split(end, 1)
            report_path.write_text(
                before + start + "\n本月早餐小额消费较集中。\n" + end + after,
                encoding="utf-8",
            )

            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")
            rebuilt = report_path.read_text(encoding="utf-8")
            self.assertIn("本月早餐小额消费较集中。", rebuilt)

    def test_init_installs_category_rules_used_during_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-01 12:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "星巴克咖啡",
                        "description": "拿铁",
                        "amount": "32.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "零钱",
                        "status": "支付成功",
                        "platform_transaction_id": "W1",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    }
                ],
            )

            self.run_cli("init", "--vault", str(vault))
            config = vault / "Finance" / "Data" / "config.json"
            self.assertTrue(config.exists())
            self.run_cli("import", "--vault", str(vault), "--file", str(source))

            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                category = connection.execute(
                    "SELECT category_primary FROM transactions"
                ).fetchone()[0]
            self.assertEqual(category, "餐饮")

    def test_screenshot_can_be_archived_before_agent_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            screenshot = Path(directory) / "huabei-july.png"
            screenshot.write_bytes(b"not-a-real-image-but-valid-archive-input")

            self.run_cli("init", "--vault", str(vault))
            self.run_cli(
                "archive",
                "--vault",
                str(vault),
                "--file",
                str(screenshot),
                "--source",
                "huabei-screenshot",
                "--coverage",
                "partial",
            )

            archived = list(
                (vault / "Finance" / "Raw" / "huabei-screenshot").glob(
                    "*-huabei-july.png"
                )
            )
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), screenshot.read_bytes())
            self.run_cli("build", "--vault", str(vault), "--month", "2026-07")
            quality = (vault / "Finance" / "Reviews" / "数据质量报告.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("原始文件：1", quality)
            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                row = connection.execute(
                    "SELECT source_platform, coverage_status FROM source_files"
                ).fetchone()
            self.assertEqual(row, ("huabei-screenshot", "partial"))

    def test_build_all_revises_original_month_after_cross_month_refund(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-06-20 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "花呗",
                        "merchant": "京东数码",
                        "description": "手机",
                        "amount": "6000.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "花呗",
                        "status": "交易成功",
                        "platform_transaction_id": "A100",
                        "merchant_order_id": "M100",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-03 12:00:00",
                        "source_platform": "alipay",
                        "account_name": "花呗",
                        "merchant": "京东数码",
                        "description": "手机退款",
                        "amount": "6000.00",
                        "direction": "income",
                        "transaction_type": "refund",
                        "payment_method": "花呗",
                        "status": "退款成功",
                        "platform_transaction_id": "A900",
                        "merchant_order_id": "M100",
                        "account_last4": "",
                    },
                ],
            )
            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))
            self.run_cli("build-all", "--vault", str(vault))

            june = (vault / "Finance" / "Months" / "2026-06.md").read_text(
                encoding="utf-8"
            )
            july = (vault / "Finance" / "Months" / "2026-07.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("total_consumption: 0.00", june)
            self.assertIn("refund_inflow: 6000.00", july)

    def test_refund_larger_than_remaining_purchase_is_not_auto_linked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory) / "vault"
            source = Path(directory) / "transactions.csv"
            self.write_normalized(
                source,
                [
                    {
                        "transaction_date": "2026-07-01 12:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "咖啡店",
                        "description": "咖啡",
                        "amount": "45.00",
                        "direction": "expense",
                        "transaction_type": "purchase",
                        "payment_method": "零钱",
                        "status": "支付成功",
                        "platform_transaction_id": "W1",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    },
                    {
                        "transaction_date": "2026-07-02 12:00:00",
                        "source_platform": "wechat",
                        "account_name": "微信支付",
                        "merchant": "咖啡店",
                        "description": "异常退款",
                        "amount": "50.00",
                        "direction": "income",
                        "transaction_type": "refund",
                        "payment_method": "零钱",
                        "status": "退款成功",
                        "platform_transaction_id": "W2",
                        "merchant_order_id": "M1",
                        "account_last4": "",
                    },
                ],
            )
            self.run_cli("init", "--vault", str(vault))
            self.run_cli("import", "--vault", str(vault), "--file", str(source))

            with sqlite3.connect(vault / "Finance" / "Data" / "finance.sqlite3") as connection:
                link_count = connection.execute("SELECT COUNT(*) FROM refund_links").fetchone()[0]
                review_count = connection.execute(
                    "SELECT COUNT(*) FROM review_items WHERE status = 'pending'"
                ).fetchone()[0]
            self.assertEqual(link_count, 0)
            self.assertEqual(review_count, 1)


if __name__ == "__main__":
    unittest.main()
