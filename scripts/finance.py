#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import difflib
import hashlib
import io
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, List, Optional


FINANCE_DIRS = (
    "Raw",
    "Data",
    "Months",
    "Accounts",
    "Reviews",
    "Assets/Charts",
    "Templates",
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    raw_id TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    source_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    original_name TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    UNIQUE(source_hash, source_platform)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    raw_id TEXT NOT NULL REFERENCES raw_records(raw_id),
    transaction_time TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    account_name TEXT NOT NULL,
    merchant_raw TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    status TEXT NOT NULL,
    platform_transaction_id TEXT NOT NULL,
    merchant_order_id TEXT NOT NULL,
    account_last4 TEXT NOT NULL,
    category_primary TEXT NOT NULL DEFAULT '待分类',
    excluded_reason TEXT,
    duplicate_of TEXT
);

CREATE TABLE IF NOT EXISTS refund_links (
    refund_id TEXT PRIMARY KEY REFERENCES transactions(transaction_id),
    purchase_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    matched_by TEXT NOT NULL,
    confidence REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    issue_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(transaction_id, issue_type)
);

CREATE TABLE IF NOT EXISTS debt_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    outstanding_minor INTEGER NOT NULL,
    due_minor INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    UNIQUE(account_name, as_of_date)
);
"""


def finance_root(vault: Path) -> Path:
    return vault.expanduser().resolve() / "Finance"


def database_path(vault: Path) -> Path:
    return finance_root(vault) / "Data" / "finance.sqlite3"


def connect(vault: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path(vault))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_vault(vault: Path) -> None:
    root = finance_root(vault)
    for relative in FINANCE_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    config_path = root / "Data" / "config.json"
    bundled_config = Path(__file__).resolve().parents[1] / "assets" / "config.example.json"
    if not config_path.exists() and bundled_config.exists():
        shutil.copy2(bundled_config, config_path)
    with connect(vault) as connection:
        connection.executescript(SCHEMA)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def amount_to_minor(value: str) -> int:
    cleaned = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("无法解析金额: {0}".format(value)) from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def decode_statement(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别账单编码；请转换为 UTF-8 或 GB18030")


def source_rows(path: Path) -> Iterable[tuple[int, Dict[str, str]]]:
    text = decode_statement(path)
    lines = text.splitlines()
    normalized_required = {
        "transaction_date",
        "source_platform",
        "merchant",
        "amount",
        "direction",
    }
    header_index = None
    source_kind = None
    for index, line in enumerate(lines):
        header = {item.strip() for item in next(csv.reader([line]))}
        if normalized_required.issubset(header):
            header_index = index
            source_kind = "normalized"
            break
        if {"交易时间", "交易分类", "交易对方", "收/支", "金额"}.issubset(header):
            header_index = index
            source_kind = "alipay"
            break
        if {"交易时间", "交易类型", "交易对方", "收/支", "金额(元)"}.issubset(header):
            header_index = index
            source_kind = "wechat"
            break
    if header_index is None or source_kind is None:
        raise ValueError("无法识别账单格式；请先转换为标准化 CSV")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    for offset, raw in enumerate(reader, start=header_index + 2):
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        if source_kind == "normalized":
            if not row.get("transaction_date"):
                continue
            yield offset, row
            continue
        if not row.get("交易时间") or not re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", row["交易时间"]):
            continue
        if source_kind == "alipay":
            yield offset, normalize_alipay_row(row)
        else:
            yield offset, normalize_wechat_row(row)


def normalize_alipay_row(row: Dict[str, str]) -> Dict[str, str]:
    searchable = " ".join(
        row.get(key, "") for key in ("交易分类", "商品说明", "交易状态", "备注")
    )
    if "退款" in searchable:
        transaction_type = "refund"
    elif "还款" in searchable:
        transaction_type = "repayment"
    elif any(keyword in searchable for keyword in ("余额宝转入", "余额宝转出", "转账")):
        transaction_type = "transfer"
    else:
        transaction_type = "purchase"
    direction_map = {"支出": "expense", "收入": "income", "收": "income", "支": "expense"}
    direction = direction_map.get(row.get("收/支", ""), "neutral")
    if transaction_type == "refund" and direction == "neutral":
        direction = "income"
    payment_method = row.get("收付款方式", "")
    digits = re.findall(r"\d", payment_method)
    return {
        "transaction_date": row.get("交易时间", "").replace("/", "-"),
        "source_platform": "alipay",
        "account_name": "支付宝",
        "merchant": row.get("交易对方", ""),
        "description": row.get("商品说明", ""),
        "amount": row.get("金额", "0"),
        "direction": direction,
        "transaction_type": transaction_type,
        "payment_method": payment_method,
        "status": row.get("交易状态", ""),
        "platform_transaction_id": row.get("交易订单号", ""),
        "merchant_order_id": row.get("商家订单号", ""),
        "account_last4": "".join(digits[-4:]) if len(digits) >= 4 else "",
    }


def normalize_wechat_row(row: Dict[str, str]) -> Dict[str, str]:
    searchable = " ".join(
        row.get(key, "") for key in ("交易类型", "商品", "当前状态", "备注")
    )
    if "退款" in searchable or "退还" in searchable:
        transaction_type = "refund"
    elif "还款" in searchable:
        transaction_type = "repayment"
    elif any(
        keyword in searchable
        for keyword in ("零钱通转入", "零钱通转出", "充值", "提现", "转账", "红包")
    ):
        transaction_type = "transfer"
    elif row.get("收/支") in ("收入", "收"):
        transaction_type = "income"
    else:
        transaction_type = "purchase"
    direction_map = {"支出": "expense", "收入": "income", "收": "income", "支": "expense"}
    direction = direction_map.get(row.get("收/支", ""), "neutral")
    if transaction_type == "refund" and direction == "neutral":
        direction = "income"
    payment_method = row.get("支付方式", "")
    digits = re.findall(r"\d", payment_method)
    return {
        "transaction_date": row.get("交易时间", "").replace("/", "-"),
        "source_platform": "wechat",
        "account_name": "微信支付",
        "merchant": row.get("交易对方", ""),
        "description": row.get("商品", ""),
        "amount": row.get("金额(元)", "0"),
        "direction": direction,
        "transaction_type": transaction_type,
        "payment_method": payment_method,
        "status": row.get("当前状态", ""),
        "platform_transaction_id": row.get("交易单号", ""),
        "merchant_order_id": row.get("商户单号", ""),
        "account_last4": "".join(digits[-4:]) if len(digits) >= 4 else "",
    }


def stable_id(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def load_config(vault: Path) -> Dict[str, object]:
    path = finance_root(vault) / "Data" / "config.json"
    if not path.exists():
        return {"category_rules": [], "merchant_overrides": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Finance/Data/config.json 不是有效 JSON") from exc


def categorize(row: Dict[str, str], config: Dict[str, object]) -> str:
    merchant = row.get("merchant", "")
    description = row.get("description", "")
    overrides = config.get("merchant_overrides", {})
    if isinstance(overrides, dict):
        override = overrides.get(merchant)
        if isinstance(override, dict) and override.get("category_primary"):
            return str(override["category_primary"])
        if isinstance(override, str) and override:
            return override
    searchable = "{0} {1}".format(merchant, description)
    rules = config.get("category_rules", [])
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("pattern", ""))
            category = str(rule.get("category_primary", ""))
            if pattern and category and re.search(pattern, searchable, re.IGNORECASE):
                return category
    return "待分类"


def archive_file(
    vault: Path,
    source_file: Path,
    source_platform: str,
    coverage_status: str,
) -> Path:
    init_vault(vault)
    source_file = source_file.expanduser().resolve()
    if not source_file.is_file():
        raise ValueError("找不到原始文件: {0}".format(source_file))
    source_hash = file_sha256(source_file)
    safe_source = re.sub(r"[^a-z0-9_-]", "-", source_platform.lower()) or "unknown"
    archive_dir = finance_root(vault) / "Raw" / safe_source
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_source = archive_dir / "{0}-{1}".format(source_hash[:12], source_file.name)
    if not archived_source.exists():
        shutil.copy2(source_file, archived_source)
    with connect(vault) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO source_files (
                source_id, source_hash, source_platform, original_name,
                archived_path, coverage_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id(source_hash, source_platform),
                source_hash,
                source_platform,
                source_file.name,
                str(archived_source),
                coverage_status,
            ),
        )
    return archived_source


def import_file(vault: Path, source_file: Path) -> int:
    init_vault(vault)
    source_file = source_file.expanduser().resolve()
    source_hash = file_sha256(source_file)
    parsed_rows = list(source_rows(source_file))
    if not parsed_rows:
        raise ValueError("账单中没有可导入的交易记录")
    source_platform = parsed_rows[0][1].get("source_platform", "unknown") or "unknown"
    archived_source = archive_file(
        vault, source_file, source_platform, "structured-export"
    )
    inserted = 0
    config = load_config(vault)
    with connect(vault) as connection:
        for source_row, row in parsed_rows:
            raw_id = stable_id(source_hash, source_row)
            transaction_id = stable_id(raw_id, row.get("platform_transaction_id", ""))
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_records
                    (raw_id, source_file, source_hash, source_row, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    raw_id,
                    str(archived_source),
                    source_hash,
                    source_row,
                    json.dumps(row, ensure_ascii=False, sort_keys=True),
                ),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO transactions (
                    transaction_id, raw_id, transaction_time, source_platform,
                    account_name, merchant_raw, description, amount_minor,
                    direction, transaction_type, payment_method, status,
                    platform_transaction_id, merchant_order_id, account_last4,
                    category_primary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    raw_id,
                    row["transaction_date"],
                    row["source_platform"],
                    row.get("account_name", ""),
                    row["merchant"],
                    row.get("description", ""),
                    amount_to_minor(row["amount"]),
                    row["direction"],
                    row.get("transaction_type", "purchase"),
                    row.get("payment_method", ""),
                    row.get("status", ""),
                    row.get("platform_transaction_id", ""),
                    row.get("merchant_order_id", ""),
                    row.get("account_last4", ""),
                    categorize(row, config),
                ),
            )
            inserted += cursor.rowcount
        match_duplicates(connection)
        match_refunds(connection)
    return inserted


def match_refunds(connection: sqlite3.Connection) -> None:
    refunds = connection.execute(
        "SELECT * FROM transactions WHERE transaction_type = 'refund'"
    ).fetchall()
    for refund in refunds:
        purchase = None
        if refund["merchant_order_id"]:
            purchase = connection.execute(
                """
                SELECT * FROM transactions
                WHERE transaction_type = 'purchase'
                  AND source_platform = ?
                  AND merchant_order_id = ?
                  AND transaction_time <= ?
                ORDER BY transaction_time DESC
                LIMIT 1
                """,
                (
                    refund["source_platform"],
                    refund["merchant_order_id"],
                    refund["transaction_time"],
                ),
            ).fetchone()
        if purchase and purchase_net_amount(connection, purchase) >= refund["amount_minor"]:
            connection.execute(
                """
                INSERT OR REPLACE INTO refund_links
                    (refund_id, purchase_id, matched_by, confidence)
                VALUES (?, ?, 'merchant_order_id', 1.0)
                """,
                (refund["transaction_id"], purchase["transaction_id"]),
            )
            connection.execute(
                "DELETE FROM review_items WHERE transaction_id = ? AND issue_type = 'refund_match'",
                (refund["transaction_id"],),
            )
            continue

        candidates = connection.execute(
            """
            SELECT * FROM transactions
            WHERE transaction_type = 'purchase'
              AND source_platform = ?
              AND transaction_time <= ?
              AND julianday(?) - julianday(transaction_time) BETWEEN 0 AND 365
            ORDER BY transaction_time DESC
            """,
            (refund["source_platform"], refund["transaction_time"], refund["transaction_time"]),
        ).fetchall()
        eligible = []
        for candidate in candidates:
            if purchase_net_amount(connection, candidate) < refund["amount_minor"]:
                continue
            if (
                refund["payment_method"]
                and candidate["payment_method"]
                and refund["payment_method"] != candidate["payment_method"]
            ):
                continue
            similarity = merchant_similarity(refund["merchant_raw"], candidate["merchant_raw"])
            if similarity >= 0.82:
                eligible.append((candidate, similarity))
        if len(eligible) == 1:
            candidate, similarity = eligible[0]
            connection.execute(
                """
                INSERT OR REPLACE INTO refund_links
                    (refund_id, purchase_id, matched_by, confidence)
                VALUES (?, ?, 'unique_merchant_amount', ?)
                """,
                (refund["transaction_id"], candidate["transaction_id"], max(0.90, similarity)),
            )
            connection.execute(
                "DELETE FROM review_items WHERE transaction_id = ? AND issue_type = 'refund_match'",
                (refund["transaction_id"],),
            )
        else:
            details = {
                "reason": "ambiguous" if len(eligible) > 1 else "no_candidate",
                "candidate_purchase_ids": [item[0]["transaction_id"] for item in eligible],
            }
            connection.execute(
                """
                INSERT OR REPLACE INTO review_items
                    (review_id, transaction_id, issue_type, details_json, status)
                VALUES (?, ?, 'refund_match', ?, 'pending')
                """,
                (
                    stable_id(refund["transaction_id"], "refund_match"),
                    refund["transaction_id"],
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                ),
            )


def match_duplicates(connection: sqlite3.Connection) -> None:
    credit_rows = connection.execute(
        """
        SELECT * FROM transactions
        WHERE transaction_type = 'purchase'
          AND source_platform = 'credit_card'
          AND excluded_reason IS NULL
        """
    ).fetchall()
    for credit in credit_rows:
        platforms = connection.execute(
            """
            SELECT * FROM transactions
            WHERE transaction_type = 'purchase'
              AND source_platform IN ('alipay', 'wechat')
              AND amount_minor = ?
              AND excluded_reason IS NULL
              AND ABS(julianday(transaction_time) - julianday(?)) <= 2
            """,
            (credit["amount_minor"], credit["transaction_time"]),
        ).fetchall()
        candidates = []
        for platform in platforms:
            if credit["account_last4"]:
                payment_digits = "".join(re.findall(r"\d", platform["payment_method"]))
                if credit["account_last4"] not in payment_digits:
                    continue
            similarity = merchant_similarity(credit["merchant_raw"], platform["merchant_raw"])
            if similarity >= 0.72:
                candidates.append(platform)
        if len(candidates) == 1:
            connection.execute(
                """
                UPDATE transactions
                SET excluded_reason = 'duplicate', duplicate_of = ?
                WHERE transaction_id = ?
                """,
                (candidates[0]["transaction_id"], credit["transaction_id"]),
            )


def merchant_similarity(left: str, right: str) -> float:
    def normalize(value: str) -> str:
        normalized = "".join(character.lower() for character in value if character.isalnum())
        for prefix in ("支付宝", "财付通", "微信支付", "微信"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        return normalized

    left_normalized = normalize(left)
    right_normalized = normalize(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    return difflib.SequenceMatcher(None, left_normalized, right_normalized).ratio()


def money(minor: int) -> str:
    return "{0:.2f}".format(Decimal(minor) / Decimal(100))


def money_display(minor: int) -> str:
    return "{0:,.2f}".format(Decimal(minor) / Decimal(100))


def save_debt_snapshot(
    vault: Path,
    account: str,
    as_of_date: str,
    outstanding: str,
    due: str,
    due_date: str,
) -> None:
    init_vault(vault)
    datetime.strptime(as_of_date, "%Y-%m-%d")
    if due_date:
        datetime.strptime(due_date, "%Y-%m-%d")
    with connect(vault) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO debt_snapshots (
                snapshot_id, account_name, as_of_date, outstanding_minor,
                due_minor, due_date
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id(account, as_of_date),
                account,
                as_of_date,
                amount_to_minor(outstanding),
                amount_to_minor(due),
                due_date,
            ),
        )


def link_refund_manually(vault: Path, refund_id: str, purchase_id: str) -> None:
    init_vault(vault)
    with connect(vault) as connection:
        refund = connection.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (refund_id,),
        ).fetchone()
        purchase = connection.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (purchase_id,),
        ).fetchone()
        if refund is None or refund["transaction_type"] != "refund":
            raise ValueError("refund-id 不是有效的退款交易")
        if purchase is None or purchase["transaction_type"] != "purchase":
            raise ValueError("purchase-id 不是有效的消费交易")
        if refund["transaction_time"] < purchase["transaction_time"]:
            raise ValueError("退款时间早于消费时间，不能关联")
        if purchase_net_amount(connection, purchase) < refund["amount_minor"]:
            raise ValueError("退款金额超过该消费尚未退款的金额")
        connection.execute(
            """
            INSERT OR REPLACE INTO refund_links
                (refund_id, purchase_id, matched_by, confidence)
            VALUES (?, ?, 'manual', 1.0)
            """,
            (refund_id, purchase_id),
        )
        connection.execute(
            """
            UPDATE review_items
            SET status = 'resolved'
            WHERE transaction_id = ? AND issue_type = 'refund_match'
            """,
            (refund_id,),
        )


def month_bounds(month: str) -> tuple[str, str]:
    try:
        start = datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise ValueError("月份必须使用 YYYY-MM 格式") from exc
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def purchase_net_amount(connection: sqlite3.Connection, purchase: sqlite3.Row) -> int:
    refunded = connection.execute(
        """
        SELECT COALESCE(SUM(t.amount_minor), 0)
        FROM refund_links rl
        JOIN transactions t ON t.transaction_id = rl.refund_id
        WHERE rl.purchase_id = ?
        """,
        (purchase["transaction_id"],),
    ).fetchone()[0]
    return max(0, purchase["amount_minor"] - refunded)


def is_debt_funded(transaction: sqlite3.Row) -> bool:
    searchable = "{0} {1} {2}".format(
        transaction["source_platform"],
        transaction["account_name"],
        transaction["payment_method"],
    ).lower()
    return any(keyword in searchable for keyword in ("credit_card", "信用卡", "花呗", "白条"))


def build_month(vault: Path, month: str) -> Path:
    init_vault(vault)
    start, end = month_bounds(month)
    output = finance_root(vault) / "Months" / "{0}.md".format(month)
    existing_report = output.read_text(encoding="utf-8") if output.exists() else ""
    analysis_content = preserved_marker_content(
        existing_report,
        "<!-- AI_ANALYSIS_START -->",
        "<!-- AI_ANALYSIS_END -->",
        "待 Agent 根据 Data/analysis-context-{0}.json 生成。".format(month),
    )
    suggestions_content = preserved_marker_content(
        existing_report,
        "<!-- AI_SUGGESTIONS_START -->",
        "<!-- AI_SUGGESTIONS_END -->",
        "待 Agent 基于可验证指标生成不超过三条建议。",
    )
    visible: List[tuple[sqlite3.Row, int]] = []
    with connect(vault) as connection:
        purchases = connection.execute(
            """
            SELECT * FROM transactions
            WHERE transaction_type = 'purchase'
              AND transaction_time >= ? AND transaction_time < ?
              AND excluded_reason IS NULL
            ORDER BY transaction_time, transaction_id
            """,
            (start, end),
        ).fetchall()
        for purchase in purchases:
            net = purchase_net_amount(connection, purchase)
            if net > 0:
                visible.append((purchase, net))
        needs_review_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM review_items r
            JOIN transactions t ON t.transaction_id = r.transaction_id
            WHERE r.status = 'pending'
              AND t.transaction_time >= ? AND t.transaction_time < ?
            """,
            (start, end),
        ).fetchone()[0]
        month_transactions = connection.execute(
            """
            SELECT * FROM transactions
            WHERE transaction_time >= ? AND transaction_time < ?
              AND excluded_reason IS NULL
            """,
            (start, end),
        ).fetchall()

    cash_outflow = 0
    refund_inflow = 0
    debt_repayment = 0
    new_debt = 0
    for transaction in month_transactions:
        transaction_type = transaction["transaction_type"]
        if transaction_type == "purchase":
            net = purchase_net_amount_from_visible(visible, transaction["transaction_id"])
            if is_debt_funded(transaction):
                new_debt += net
            elif transaction["direction"] == "expense":
                cash_outflow += transaction["amount_minor"]
        elif transaction_type in ("repayment", "installment"):
            debt_repayment += transaction["amount_minor"]
            if transaction["direction"] in ("expense", "neutral"):
                cash_outflow += transaction["amount_minor"]
        elif transaction_type in ("fee", "interest", "cash_withdrawal"):
            if transaction["direction"] == "expense":
                cash_outflow += transaction["amount_minor"]
        elif transaction_type == "refund" and transaction["direction"] == "income":
            refund_inflow += transaction["amount_minor"]

    total = sum(net for _, net in visible)
    lines = [
        "---",
        "type: finance-month",
        "month: {0}".format(month),
        "total_consumption: {0}".format(money(total)),
        "cash_outflow: {0}".format(money(cash_outflow)),
        "refund_inflow: {0}".format(money(refund_inflow)),
        "debt_repayment: {0}".format(money(debt_repayment)),
        "new_debt: {0}".format(money(new_debt)),
        "needs_review_count: {0}".format(needs_review_count),
        "---",
        "",
        "# {0} 财务报告".format(month),
        "",
        "## 本月概览",
        "",
        "- 本月消费：¥{0}".format(money(total)),
        "- 实际现金支出：¥{0}".format(money(cash_outflow)),
        "- 退款流入：¥{0}".format(money(refund_inflow)),
        "- 债务还款：¥{0}".format(money(debt_repayment)),
        "- 新增负债：¥{0}".format(money(new_debt)),
        "",
        "## 消费明细",
        "",
        "| 日期 | 商户 | 分类 | 净消费 |",
        "| --- | --- | --- | ---: |",
    ]
    for transaction, net in visible:
        lines.append(
            "| {0} | {1} | {2} | ¥{3} |".format(
                transaction["transaction_time"][:10],
                transaction["merchant_raw"].replace("|", "\\|"),
                transaction["category_primary"],
                money(net),
            )
        )
    if not visible:
        lines.append("| — | 本月没有净消费记录 | — | ¥0.00 |")
    lines.extend(["", "## 数据质量", "", "- 报告由原始记录派生；原始记录未删除。", ""])
    lines[-4:-4] = [
        "",
        "## 消费习惯分析",
        "",
        "<!-- AI_ANALYSIS_START -->",
        analysis_content,
        "<!-- AI_ANALYSIS_END -->",
        "",
        "## 下月建议",
        "",
        "<!-- AI_SUGGESTIONS_START -->",
        suggestions_content,
        "<!-- AI_SUGGESTIONS_END -->",
        "",
    ]

    output.write_text("\n".join(lines), encoding="utf-8")
    build_review_and_quality_pages(vault)
    write_analysis_context(
        vault,
        month,
        visible,
        total,
        cash_outflow,
        refund_inflow,
        debt_repayment,
        new_debt,
        needs_review_count,
    )
    build_dashboard(
        vault,
        month,
        total,
        cash_outflow,
        debt_repayment,
        new_debt,
        needs_review_count,
    )
    return output


def preserved_marker_content(text: str, start_marker: str, end_marker: str, default: str) -> str:
    if start_marker not in text or end_marker not in text:
        return default
    content = text.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    return content or default


def build_review_and_quality_pages(vault: Path) -> None:
    with connect(vault) as connection:
        reviews = connection.execute(
            """
            SELECT r.*, t.transaction_time, t.merchant_raw, t.amount_minor,
                   t.source_platform, t.platform_transaction_id
            FROM review_items r
            JOIN transactions t ON t.transaction_id = r.transaction_id
            WHERE r.status = 'pending'
            ORDER BY t.transaction_time, r.review_id
            """
        ).fetchall()
        raw_count = connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
        transaction_count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        source_count = connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0]
        duplicate_count = connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE excluded_reason = 'duplicate'"
        ).fetchone()[0]
        linked_refund_count = connection.execute(
            "SELECT COUNT(*) FROM refund_links"
        ).fetchone()[0]

        review_lines = [
            "---",
            "type: finance-review",
            "pending_count: {0}".format(len(reviews)),
            "---",
            "",
            "# 待确认交易",
            "",
        ]
        if not reviews:
            review_lines.append("当前没有待确认交易。")
        for review in reviews:
            details = json.loads(review["details_json"])
            review_lines.extend(
                [
                    "## {0} · {1} · ¥{2}".format(
                        review["transaction_time"][:10],
                        review["merchant_raw"],
                        money_display(review["amount_minor"]),
                    ),
                    "",
                    "- 退款交易 ID：`{0}`".format(review["transaction_id"]),
                    "- 平台交易号：`{0}`".format(
                        review["platform_transaction_id"] or "缺失"
                    ),
                    "- 原因：{0}".format(details.get("reason", "unknown")),
                    "- 候选消费：",
                ]
            )
            candidate_ids = details.get("candidate_purchase_ids", [])
            if not candidate_ids:
                review_lines.append("  - 无候选；需要补充账单或人工查找。")
            for candidate_id in candidate_ids:
                candidate = connection.execute(
                    """
                    SELECT transaction_time, merchant_raw, amount_minor
                    FROM transactions WHERE transaction_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
                if candidate:
                    review_lines.append(
                        "  - `{0}` · {1} · {2} · ¥{3}".format(
                            candidate_id,
                            candidate["transaction_time"][:10],
                            candidate["merchant_raw"],
                            money_display(candidate["amount_minor"]),
                        )
                    )
            review_lines.extend(
                [
                    "",
                    "确认后运行：",
                    "",
                    "```bash",
                    "python3 scripts/finance.py link-refund --vault <VAULT> --refund-id {0} --purchase-id <PURCHASE_ID>".format(
                        review["transaction_id"]
                    ),
                    "```",
                    "",
                ]
            )

    reviews_dir = finance_root(vault) / "Reviews"
    (reviews_dir / "待确认交易.md").write_text(
        "\n".join(review_lines), encoding="utf-8"
    )
    quality_lines = [
        "---",
        "type: finance-data-quality",
        "---",
        "",
        "# 数据质量报告",
        "",
        "- 原始文件：{0}".format(source_count),
        "- 原始记录：{0}".format(raw_count),
        "- 标准化交易：{0}".format(transaction_count),
        "- 跨平台重复：{0}".format(duplicate_count),
        "- 已关联退款：{0}".format(linked_refund_count),
        "- 待确认交易：{0}".format(len(reviews)),
        "",
        "> 截图或平台当前可见账单可能不覆盖已删除记录；没有账单汇总值时，不能声明已经完成金额勾稽。",
        "",
    ]
    (reviews_dir / "数据质量报告.md").write_text(
        "\n".join(quality_lines), encoding="utf-8"
    )


def write_analysis_context(
    vault: Path,
    month: str,
    visible: List[tuple[sqlite3.Row, int]],
    total: int,
    cash_outflow: int,
    refund_inflow: int,
    debt_repayment: int,
    new_debt: int,
    needs_review_count: int,
) -> Path:
    categories: Dict[str, int] = defaultdict(int)
    merchants: Dict[str, int] = defaultdict(int)
    small_purchase_count = 0
    small_purchase_minor = 0
    for transaction, net in visible:
        categories[transaction["category_primary"]] += net
        merchants[transaction["merchant_raw"]] += net
        if net < 5000:
            small_purchase_count += 1
            small_purchase_minor += net
    payload = {
        "month": month,
        "metrics": {
            "total_consumption": money(total),
            "cash_outflow": money(cash_outflow),
            "refund_inflow": money(refund_inflow),
            "debt_repayment": money(debt_repayment),
            "new_debt": money(new_debt),
        },
        "categories": [
            {"name": name, "amount": money(amount)}
            for name, amount in sorted(categories.items(), key=lambda item: item[1], reverse=True)
        ],
        "top_merchants": [
            {"name": name, "amount": money(amount)}
            for name, amount in sorted(merchants.items(), key=lambda item: item[1], reverse=True)[:10]
        ],
        "small_purchases": {
            "threshold": "50.00",
            "count": small_purchase_count,
            "amount": money(small_purchase_minor),
        },
        "needs_review_count": needs_review_count,
        "analysis_guardrail": "Only state findings supported by these fields; label incomplete coverage.",
    }
    output = finance_root(vault) / "Data" / "analysis-context-{0}.json".format(month)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def purchase_net_amount_from_visible(
    visible: List[tuple[sqlite3.Row, int]], transaction_id: str
) -> int:
    for transaction, net in visible:
        if transaction["transaction_id"] == transaction_id:
            return net
    return 0


def build_dashboard(
    vault: Path,
    month: str,
    total: int,
    cash_outflow: int,
    debt_repayment: int,
    new_debt: int,
    needs_review_count: int,
) -> Path:
    with connect(vault) as connection:
        debts = connection.execute(
            """
            SELECT d.*
            FROM debt_snapshots d
            JOIN (
                SELECT account_name, MAX(as_of_date) AS latest
                FROM debt_snapshots
                GROUP BY account_name
            ) current
              ON current.account_name = d.account_name
             AND current.latest = d.as_of_date
            ORDER BY d.account_name
            """
        ).fetchall()
    lines = [
        "---",
        "type: finance-dashboard",
        "current_month: {0}".format(month),
        "---",
        "",
        "# 个人财务 Dashboard",
        "",
        "## 本月核心数字",
        "",
        "| 月份 | 本月消费 | 现金支出 | 债务还款 | 新增负债 | 待确认 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        "| [[Months/{0}|{0}]] | ¥{1} | ¥{2} | ¥{3} | ¥{4} | {5} |".format(
            month,
            money_display(total),
            money_display(cash_outflow),
            money_display(debt_repayment),
            money_display(new_debt),
            needs_review_count,
        ),
        "",
        "## 债务与待还款",
        "",
        "| 账户 | 截止日期 | 剩余待还 | 本期应还 | 最近还款日 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for debt in debts:
        lines.append(
            "| {0} | {1} | ¥{2} | ¥{3} | {4} |".format(
                debt["account_name"],
                debt["as_of_date"],
                money_display(debt["outstanding_minor"]),
                money_display(debt["due_minor"]),
                debt["due_date"] or "—",
            )
        )
    if not debts:
        lines.append("| — | — | ¥0.00 | ¥0.00 | — |")
    lines.extend(
        [
            "",
            "## 快速入口",
            "",
            "- [[Reviews/待确认交易|待确认交易]]",
            "- [[Reviews/数据质量报告|数据质量报告]]",
            "",
        ]
    )
    output = finance_root(vault) / "Dashboard.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def build_all_months(vault: Path) -> List[Path]:
    init_vault(vault)
    with connect(vault) as connection:
        months = [
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT substr(transaction_time, 1, 7) AS month
                FROM transactions
                WHERE transaction_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*'
                ORDER BY month
                """
            ).fetchall()
        ]
    return [build_month(vault, month) for month in months]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal finance → Obsidian processor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_command = subparsers.add_parser("init", help="Initialize Finance in an Obsidian vault")
    init_command.add_argument("--vault", required=True, type=Path)

    import_command = subparsers.add_parser("import", help="Import a normalized CSV")
    import_command.add_argument("--vault", required=True, type=Path)
    import_command.add_argument("--file", required=True, type=Path)

    build_command = subparsers.add_parser("build", help="Build a monthly Obsidian report")
    build_command.add_argument("--vault", required=True, type=Path)
    build_command.add_argument("--month", required=True)

    build_all_command = subparsers.add_parser(
        "build-all", help="Rebuild every affected natural month"
    )
    build_all_command.add_argument("--vault", required=True, type=Path)

    debt_command = subparsers.add_parser("debt", help="Record a debt snapshot")
    debt_command.add_argument("--vault", required=True, type=Path)
    debt_command.add_argument("--account", required=True)
    debt_command.add_argument("--as-of", required=True)
    debt_command.add_argument("--outstanding", required=True)
    debt_command.add_argument("--due", default="0")
    debt_command.add_argument("--due-date", default="")

    link_refund_command = subparsers.add_parser(
        "link-refund", help="Manually link an ambiguous refund to a purchase"
    )
    link_refund_command.add_argument("--vault", required=True, type=Path)
    link_refund_command.add_argument("--refund-id", required=True)
    link_refund_command.add_argument("--purchase-id", required=True)

    archive_command = subparsers.add_parser(
        "archive", help="Archive a PDF, image, or unsupported original before normalization"
    )
    archive_command.add_argument("--vault", required=True, type=Path)
    archive_command.add_argument("--file", required=True, type=Path)
    archive_command.add_argument("--source", required=True)
    archive_command.add_argument(
        "--coverage", choices=("complete", "partial", "unknown"), default="unknown"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_vault(args.vault)
            print("已初始化: {0}".format(finance_root(args.vault)))
        elif args.command == "import":
            count = import_file(args.vault, args.file)
            print("已导入 {0} 笔交易".format(count))
        elif args.command == "build":
            output = build_month(args.vault, args.month)
            print("已生成: {0}".format(output))
        elif args.command == "build-all":
            outputs = build_all_months(args.vault)
            print("已生成 {0} 个月度报告".format(len(outputs)))
        elif args.command == "debt":
            save_debt_snapshot(
                args.vault,
                args.account,
                args.as_of,
                args.outstanding,
                args.due,
                args.due_date,
            )
            print("已保存负债快照: {0} {1}".format(args.account, args.as_of))
        elif args.command == "link-refund":
            link_refund_manually(args.vault, args.refund_id, args.purchase_id)
            print("已人工关联退款: {0} -> {1}".format(args.refund_id, args.purchase_id))
        elif args.command == "archive":
            output = archive_file(args.vault, args.file, args.source, args.coverage)
            print("已归档原始文件: {0}".format(output))
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print("错误: {0}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
