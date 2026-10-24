#!/usr/bin/env python3
from __future__ import annotations

import argparse
from functools import wraps
from finance_storage import atomic_write_text, migrate_database, vault_lock
import finance_matching as matching
from collections import defaultdict
import csv
import difflib
import hashlib
import io
import os
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
    "Imports",
    "Backups",
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


def locked(function):
    @wraps(function)
    def wrapper(vault, *args, **kwargs):
        with vault_lock(finance_root(vault)):
            return function(vault, *args, **kwargs)
    return wrapper


@locked
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
        migrate_database(connection, root / "Backups")


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
    if not amount.is_finite() or amount < 0:
        raise ValueError("金额必须为非负有限数")
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
        if None in raw:
            raise ValueError("第 {0} 行列数超过表头".format(offset))
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        if source_kind == "normalized":
            if not any(row.values()):
                continue
            yield offset, row
            continue
        if not row.get("交易时间"):
            if any(row.get(key) for key in ('交易对方','金额','金额(元)')):
                raise ValueError('第 {0} 行缺少日期'.format(offset))
            continue
        if not re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}",row['交易时间']):
            if row['交易时间'].startswith(('---','共','合计','统计')):
                continue
            raise ValueError('第 {0} 行日期无效'.format(offset))
        if source_kind == "alipay":
            normalized = normalize_alipay_row(row)
            normalized["_source_raw"] = row
            yield offset, normalized
        else:
            normalized = normalize_wechat_row(row)
            normalized["_source_raw"] = row
            yield offset, normalized


def normalize_alipay_row(row: Dict[str, str]) -> Dict[str, str]:
    searchable = " ".join(
        row.get(key, "") for key in ("交易分类", "商品说明", "交易状态", "备注")
    )
    if "退款" in searchable and row.get("收/支") not in ("支出", "支"):
        transaction_type = "refund"
    elif "还款" in searchable:
        transaction_type = "repayment"
    elif any(keyword in searchable for keyword in ("余额宝转入", "余额宝转出", "转账")):
        transaction_type = "transfer"
    elif any(keyword in searchable for keyword in ('余额宝收益','零钱通收益','投资收益')):
        transaction_type = 'investment_income'
    elif row.get('收/支') in ('收入','收'):
        transaction_type = 'income'
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
    if ("退款" in searchable or "退还" in searchable) and row.get("收/支") not in ("支出", "支"):
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
        config=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config,dict) or not isinstance(config.get('category_rules',[]),list) or not isinstance(config.get('merchant_overrides',{}),dict):
            raise ValueError('Finance/Data/config.json 配置结构无效')
        for rule in config.get('category_rules',[]):
            if not isinstance(rule,dict) or not rule.get('pattern') or not rule.get('category_primary'):
                raise ValueError('config.json 分类规则缺少字段')
            try:
                re.compile(rule['pattern'])
            except (re.error,TypeError) as exc:
                raise ValueError('config.json 分类规则正则无效') from exc
        return config
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


@locked
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


ALLOWED_TYPES = set("purchase refund transfer repayment installment fee interest cash_withdrawal income investment_buy investment_sell investment_income topup withdrawal adjustment unknown".split())


def validate_row(row, source_row):
    try:
        value = row.get("transaction_date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", value):
            raise ValueError("日期格式无效")
        datetime.fromisoformat(value)
        amount_to_minor(row["amount"])
        if row.get("direction") not in ("expense", "income", "neutral"):
            raise ValueError("direction 无效")
        if row.get("transaction_type", "purchase") not in ALLOWED_TYPES:
            raise ValueError("transaction_type 无效")
        if row.get("currency", "CNY") not in ("", "CNY"):
            raise ValueError("仅支持 CNY 币种")
        if not row.get("source_platform"):
            raise ValueError("缺少 source_platform")
        if row.get("settlement", "unknown") not in ("", "unknown", "cash", "debt_only"):
            raise ValueError("settlement 无效")
        if row.get("transfer_scope", "unknown") not in ("", "unknown", "internal", "external"):
            raise ValueError("transfer_scope 无效")
    except (ValueError, KeyError, InvalidOperation, OverflowError) as exc:
        raise ValueError("第 {0} 行: {1}".format(source_row, exc)) from exc


def event_identity(row, raw_id):
    if row.get("platform_transaction_id") and row.get("account_name"):
        return stable_id("event", row["source_platform"], row["account_name"], row.get("account_last4", ""), row["platform_transaction_id"])
    return stable_id("observation", raw_id)


def _totals(connection):
    totals = defaultdict(int)
    for row in connection.execute("SELECT * FROM transactions WHERE active=1 AND transaction_type='purchase' AND active=1 AND excluded_reason IS NULL"):
        totals[row["transaction_time"][:7]] += purchase_net_amount(connection, row)
    return dict(totals)


def persist_receipt(vault: Path, name: str, receipt: dict) -> None:
    try:
        atomic_write_text(finance_root(vault)/'Imports'/name,json.dumps(receipt,ensure_ascii=False,indent=2))
    except OSError as exc:
        message='总账已更新，回执/页面待重建；请运行 build-all，勿重复入账: '+str(exc)
        with connect(vault) as con:
            con.execute("UPDATE report_state SET status='failed',error=?",(message,))
        raise ValueError(message) from exc


@locked
def import_with_receipt(vault: Path, source_file: Path, *, coverage="unknown", source_reference="", reactivate=False, account_name="") -> dict:
    if coverage not in ("complete", "partial", "unknown"):
        raise ValueError("coverage 无效")
    init_vault(vault)
    source_file = source_file.expanduser().resolve()
    source_hash = file_sha256(source_file)
    # Archive before parsing, including hard failures. The archive alone is not
    # a successful accounting batch.
    try:
        parsed = list(source_rows(source_file))
        if not parsed:
            raise ValueError("账单中没有可导入的交易记录")
    except (ValueError, UnicodeError, csv.Error):
        archive_file(vault,source_file,'incoming',coverage)
        raise
    platform = parsed[0][1].get("source_platform") or 'incoming'
    archived = archive_file(vault, source_file, platform, coverage)
    for line, row in parsed:
        if account_name:
            if '_source_raw' not in row:
                row['_source_raw']=dict(row)
            row['account_name']=account_name
        validate_row(row, line)
    config = load_config(vault)
    with connect(vault) as con:
        before = _totals(con)
        before_links={tuple(r) for r in con.execute('SELECT refund_id,purchase_id FROM refund_links')}
        batch = con.execute("SELECT * FROM import_batches WHERE source_hash=? AND account_scope=?", (source_hash,account_name)).fetchone()
        if batch and batch["status"] == "undone" and not reactivate:
            raise ValueError("此文件批次已撤销；需明确重新纳入（reactivate）")
        if batch and batch["status"] == "active":
            return dict(batch_id=batch["batch_id"], inserted_count=0, duplicate_count=len(parsed), affected_months=[], status="already_imported", consumption_changes={}, artifact_status=con.execute('SELECT status FROM report_state').fetchone()[0],source_name=source_file.name,source_file=batch['source_file'],created_at=batch['created_at'])
        batch_id = batch["batch_id"] if batch else stable_id("batch", source_hash,account_name)
        con.execute("INSERT OR IGNORE INTO import_batches(batch_id,source_hash,source_file,coverage,source_reference,created_at,account_scope) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, source_hash, str(archived), coverage, source_reference, datetime.now().isoformat(),account_name))
        con.execute("UPDATE import_batches SET status='active' WHERE batch_id=?", (batch_id,))
        inserted = 0
        for line, row in parsed:
            raw_id = stable_id(source_hash,line,account_name) if account_name else stable_id(source_hash,line)
            tid = event_identity(row, raw_id)
            # Legacy IDs remain stable after migration.
            if row.get("platform_transaction_id") and row.get("account_name"):
                old = con.execute("SELECT * FROM transactions WHERE source_platform=? AND account_name=? AND account_last4=? AND platform_transaction_id=? AND COALESCE(excluded_reason,'')!='identity_duplicate' ORDER BY transaction_id LIMIT 1", (row["source_platform"],row["account_name"],row.get("account_last4", ""),row["platform_transaction_id"])).fetchone()
                if old:
                    tid = old["transaction_id"]
            con.execute("INSERT OR IGNORE INTO raw_records VALUES(?,?,?,?,?)", (raw_id, str(archived), source_hash, line, json.dumps(row, ensure_ascii=False, sort_keys=True)))
            existing = con.execute("SELECT * FROM transactions WHERE transaction_id=?", (tid,)).fetchone()
            if existing:
                pairs = (("transaction_time","transaction_date"),("merchant_raw","merchant"),("direction","direction"),("transaction_type","transaction_type"))
                conflict = any(existing[a] != row.get(b, "purchase" if b == "transaction_type" else "") for a,b in pairs) or existing["amount_minor"] != amount_to_minor(row["amount"])
                if conflict:
                    con.execute("INSERT OR REPLACE INTO review_items VALUES(?,?, 'source_conflict', ?, 'pending')", (stable_id(tid,"source_conflict"), tid, json.dumps({"reason":"同账户交易号内容冲突", "raw_id":raw_id}, ensure_ascii=False)))
            else:
                values = (tid, raw_id, row["transaction_date"], row["source_platform"], row.get("account_name", ""), row["merchant"], row.get("description", ""), amount_to_minor(row["amount"]), row["direction"], row.get("transaction_type", "purchase"), row.get("payment_method", ""), row.get("status", ""), row.get("platform_transaction_id", ""), row.get("merchant_order_id", ""), row.get("account_last4", ""), categorize(row, config), row.get("settlement", "unknown") or "unknown", row.get("source_reference",source_reference), row.get("source_locator", ""), row.get("transfer_scope", "unknown") or "unknown")
                con.execute("""INSERT INTO transactions(transaction_id,raw_id,transaction_time,source_platform,account_name,merchant_raw,description,amount_minor,direction,transaction_type,payment_method,status,platform_transaction_id,merchant_order_id,account_last4,category_primary,settlement,source_reference,source_locator,transfer_scope) VALUES(""" + ','.join('?' for _ in values) + ')', values)
                fresh=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(tid,)).fetchone()
                con.execute('UPDATE transactions SET category_primary=? WHERE transaction_id=?',(effective_category(con,fresh,config),tid))
                inserted += 1
            con.execute("INSERT OR IGNORE INTO transaction_observations VALUES(?,?,?)", (raw_id,tid,batch_id))
        con.execute("UPDATE transactions SET active=EXISTS(SELECT 1 FROM transaction_observations o JOIN import_batches b ON b.batch_id=o.batch_id WHERE o.transaction_id=transactions.transaction_id AND b.status='active')")
        match_duplicates(con)
        match_refunds(con)
        after = _totals(con)
        affected = sorted(set(row['transaction_date'][:7] for _,row in parsed) | {m for m in set(before)|set(after) if before.get(m,0)!=after.get(m,0)})
        receipt = dict(batch_id=batch_id, inserted_count=inserted, duplicate_count=len(parsed)-inserted, affected_months=affected, status="imported", consumption_changes={m:dict(before=money(before.get(m,0)),after=money(after.get(m,0)),delta=money(after.get(m,0)-before.get(m,0))) for m in affected}, artifact_status="pending")
        receipt.update(source_name=source_file.name,source_file=str(archived),created_at=datetime.now().isoformat())
        after_links={tuple(r) for r in con.execute('SELECT refund_id,purchase_id FROM refund_links')}
        receipt['linked_refund_count']=len(after_links-before_links)
        receipt['unlinked_refund_count']=len(before_links-after_links)
        receipt['total_linked_refund_count']=con.execute('SELECT COUNT(*) FROM refund_links l JOIN transactions t ON t.transaction_id=l.refund_id WHERE t.active=1').fetchone()[0]
        receipt['account_scope']=account_name
        receipt['review_count']=len(matching.list_reviews(con))
        receipt['artifacts']=[str(finance_root(vault)/'Months'/(m+'.md')) for m in affected]+[str(finance_root(vault)/'Dashboard.md')]
        con.execute("UPDATE import_batches SET receipt_json=? WHERE batch_id=?", (json.dumps(receipt,ensure_ascii=False),batch_id))
        con.execute("UPDATE report_state SET status='pending', error='' WHERE singleton=1")
    persist_receipt(vault,batch_id+'.json',receipt)
    return receipt


def import_file(vault: Path, source_file: Path) -> int:
    return import_with_receipt(vault, source_file)["inserted_count"]


match_refunds = matching.match_refunds
match_duplicates = matching.match_duplicates
merchant_similarity = matching.merchant_similarity


def list_reviews(vault: Path) -> list:
    with connect_readonly(vault) as con:
        return matching.list_reviews(con)


def money(minor: int) -> str:
    return "{0:.2f}".format(Decimal(minor) / Decimal(100))


def money_display(minor: int) -> str:
    return "{0:,.2f}".format(Decimal(minor) / Decimal(100))


@locked
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
                amount_to_minor(outstanding) if outstanding else None,
                amount_to_minor(due) if due else None,
                due_date,
            ),
        )
        connection.execute("UPDATE report_state SET status='pending'")


@locked
def link_refund_manually(vault: Path, refund_id: str, purchase_id: str, *, snapshot=None) -> None:
    init_vault(vault)
    with connect(vault) as con:
        if snapshot is not None:
            current = [r for r in matching.list_reviews(con) if r['transaction_id']==refund_id and r['snapshot']==snapshot]
            if not current or purchase_id not in {c['transaction_id'] for r in current for c in r['candidates']}:
                raise ValueError('候选列表已变化，请重新展示并确认')
        refund = con.execute('SELECT * FROM transactions WHERE transaction_id=?',(refund_id,)).fetchone()
        purchase = con.execute('SELECT * FROM transactions WHERE transaction_id=?',(purchase_id,)).fetchone()
        if not refund or refund['transaction_type']!='refund' or not refund['active'] or refund['excluded_reason']:
            raise ValueError('refund-id 不是有效的退款交易')
        if not purchase or purchase['transaction_type']!='purchase' or not purchase['active'] or purchase['excluded_reason']:
            raise ValueError('purchase-id 不是有效的规范消费交易')
        if refund['transaction_time']<purchase['transaction_time']:
            raise ValueError('退款时间早于消费时间，不能关联')
        old = con.execute('SELECT * FROM refund_links WHERE refund_id=?',(refund_id,)).fetchone()
        available = purchase_net_amount(con,purchase)
        if old and old['purchase_id']==purchase_id:
            available += refund['amount_minor']
        if refund['amount_minor']>available:
            raise ValueError('退款金额超过该消费尚未退款的金额')
        con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('link_refund',?,?,?)",(refund_id,json.dumps(dict(purchase_id=purchase_id,candidates=matching.refund_candidates(con,refund)[0])),datetime.now().isoformat()))
        con.execute("UPDATE decisions SET active=0 WHERE kind IN ('unlink_refund','migration_refund_conflict') AND target_id=?",(refund_id,))
        con.execute('DELETE FROM refund_rejections WHERE refund_id=? AND purchase_id=?',(refund_id,purchase_id))
        con.execute("INSERT OR REPLACE INTO refund_links VALUES(?,?,'manual',1.0)",(refund_id,purchase_id))
        con.execute("UPDATE review_items SET status='resolved' WHERE transaction_id=? AND issue_type IN ('refund_match','manual_conflict')",(refund_id,))
        con.execute("UPDATE report_state SET status='pending'")


@locked
def unlink_refund(vault: Path, refund_id: str) -> None:
    with connect(vault) as con:
        link = con.execute('SELECT * FROM refund_links WHERE refund_id=?',(refund_id,)).fetchone()
        if link:
            con.execute('INSERT OR IGNORE INTO refund_rejections VALUES(?,?)',(refund_id,link['purchase_id']))
            con.execute('DELETE FROM refund_links WHERE refund_id=?',(refund_id,))
        con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('unlink_refund',?,'{}',?)",(refund_id,datetime.now().isoformat()))
        match_refunds(con)
        con.execute("UPDATE report_state SET status='pending'")


def effective_category(connection, row, config):
    if row['category_manual']:
        return row['category_manual']
    rules = connection.execute("SELECT category FROM category_rules WHERE merchant=? AND effective_from<=? ORDER BY rule_id DESC LIMIT 1",(row['merchant_raw'],row['transaction_time'])).fetchone()
    if rules:
        return rules[0]
    return categorize({'merchant':row['merchant_raw'],'description':row['description']},config)


@locked
def reclassify(vault: Path) -> dict:
    config = load_config(vault)
    count = 0
    months=set()
    with connect(vault) as con:
        for row in con.execute('SELECT * FROM transactions').fetchall():
            category = effective_category(con,row,config)
            if row['category_primary']!=category:
                con.execute('UPDATE transactions SET category_primary=? WHERE transaction_id=?',(category,row['transaction_id']))
                count+=1
                months.add(row['transaction_time'][:7])
        con.execute("UPDATE report_state SET status='pending'")
    return dict(updated_count=count,affected_months=sorted(months),artifact_status='pending')


@locked
def set_category(vault: Path, transaction_id: str, category: str, scope='transaction') -> dict:
    if scope not in ('transaction','merchant-future','merchant-all') or not category.strip():
        raise ValueError('分类或作用范围无效')
    with connect(vault) as con:
        row=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(transaction_id,)).fetchone()
        if not row:
            raise ValueError('找不到交易')
        if row['duplicate_of']:
            transaction_id=row['duplicate_of']
            row=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(transaction_id,)).fetchone()
        value=dict(category=category,scope=scope,previous=row['category_manual'])
        if scope=='transaction':
            con.execute('UPDATE transactions SET category_manual=?,category_primary=? WHERE transaction_id=?',(category,category,transaction_id))
        else:
            effective = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if scope=='merchant-future' else ''
            rule=con.execute('INSERT INTO category_rules(merchant,category,effective_from) VALUES(?,?,?)',(row['merchant_raw'],category,effective))
            value.update(rule_id=rule.lastrowid,effective_from=effective)
        decision=con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('category',?,?,?)",(transaction_id,json.dumps(value,ensure_ascii=False),datetime.now().isoformat())).lastrowid
        con.execute("UPDATE report_state SET status='pending'")
    # Future rules also apply to any already-present future-dated rows.
    result=reclassify(vault)
    if scope=='transaction' and row['category_primary']!=category:
        result['updated_count']+=1
        result['affected_months']=sorted(set(result['affected_months'])|{row['transaction_time'][:7]})
    result.update(decision_id=decision, transaction_id=transaction_id,scope=scope)
    return result


@locked
def undo_decision(vault: Path, decision_id: int) -> dict:
    with connect(vault) as con:
        row=con.execute('SELECT * FROM decisions WHERE decision_id=?',(decision_id,)).fetchone()
        if not row:
            raise ValueError('找不到人工决定')
        if not row['active']:
            return dict(status='already_undone')
        if row['kind']=='review_patch':
            value=json.loads(row['value_json'])
            # Undoing an older patch must not overwrite a later active patch.
            later=con.execute("SELECT value_json FROM decisions WHERE kind='review_patch' AND target_id=? AND active=1 AND decision_id>?",(row['target_id'],decision_id)).fetchall()
            later_fields={key for r in later for key in json.loads(r[0])['after']}
            for key,old in value['before'].items():
                if key not in later_fields:
                    earlier=con.execute("SELECT value_json FROM decisions WHERE kind='review_patch' AND target_id=? AND active=1 AND decision_id<? ORDER BY decision_id DESC",(row['target_id'],decision_id)).fetchall()
                    old=next((json.loads(r[0])['after'][key] for r in earlier if key in json.loads(r[0])['after']),old)
                    con.execute('UPDATE transactions SET '+key+'=? WHERE transaction_id=?',(old,row['target_id']))
            con.execute('UPDATE decisions SET active=0 WHERE decision_id=?',(decision_id,))
            match_duplicates(con);match_refunds(con)
            con.execute("UPDATE report_state SET status='pending'")
            return dict(status='undone',artifact_status='pending')
        if row['kind']!='category':
            raise ValueError('此决定请使用对应的解除/批次撤销操作')
        value=json.loads(row['value_json'])
        con.execute('UPDATE decisions SET active=0 WHERE decision_id=?',(decision_id,))
        if value['scope']=='transaction':
            previous=con.execute("SELECT value_json FROM decisions WHERE kind='category' AND target_id=? AND active=1 ORDER BY decision_id DESC",(row['target_id'],)).fetchall()
            previous=next((json.loads(r[0])['category'] for r in previous if json.loads(r[0])['scope']=='transaction'),None)
            con.execute('UPDATE transactions SET category_manual=? WHERE transaction_id=?',(previous,row['target_id']))
        else:
            con.execute('DELETE FROM category_rules WHERE rule_id=?',(value['rule_id'],))
        con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('undo_decision',?,?,?)",(str(decision_id),'{}',datetime.now().isoformat()))
    result=reclassify(vault)
    result['status']='undone'
    return result


@locked
def undo_import(vault: Path, batch_id: str) -> dict:
    with connect(vault) as con:
        batch=con.execute('SELECT * FROM import_batches WHERE batch_id=?',(batch_id,)).fetchone()
        if not batch:
            raise ValueError('找不到导入批次')
        if batch['status']=='undone':
            return dict(batch_id=batch_id,status='already_undone',affected_months=[],consumption_changes={})
        before=_totals(con)
        con.execute("UPDATE import_batches SET status='undone' WHERE batch_id=?",(batch_id,))
        con.execute("UPDATE transactions SET active=EXISTS(SELECT 1 FROM transaction_observations o JOIN import_batches b ON b.batch_id=o.batch_id WHERE o.transaction_id=transactions.transaction_id AND b.status='active')")
        match_duplicates(con)
        match_refunds(con)
        after=_totals(con)
        months={r[0] for r in con.execute('SELECT substr(t.transaction_time,1,7) FROM transactions t JOIN transaction_observations o ON o.transaction_id=t.transaction_id WHERE o.batch_id=?',(batch_id,))}
        affected=sorted(months|{m for m in set(before)|set(after) if before.get(m,0)!=after.get(m,0)})
        receipt=dict(batch_id=batch_id,status='undone',affected_months=affected,consumption_changes={m:dict(before=money(before.get(m,0)),after=money(after.get(m,0)),delta=money(after.get(m,0)-before.get(m,0))) for m in affected},artifact_status='pending')
        decision=con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('undo_import',?,?,?)",(batch_id,json.dumps(receipt,ensure_ascii=False),datetime.now().isoformat())).lastrowid
        con.execute("UPDATE report_state SET status='pending'")
    persist_receipt(vault,'undo-'+str(decision)+'.json',receipt)
    return receipt


@locked
def resolve_review(vault: Path, review_id: str, snapshot: str, changes: dict) -> dict:
    allowed={'transaction_type','direction','settlement','transfer_scope','status','duplicate_of','raw_id'}
    if not changes or set(changes)-allowed or not all(isinstance(v,str) for v in changes.values()):
        raise ValueError('复核字段无效')
    with connect(vault) as con:
        item=next((r for r in matching.list_reviews(con) if r['review_id']==review_id and r['snapshot']==snapshot),None)
        if not item:
            raise ValueError('候选/问题列表已变化，请重新展示并确认')
        tid=item['transaction_id'];row=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(tid,)).fetchone()
        normalized=dict(transaction_date=row['transaction_time'],source_platform=row['source_platform'],merchant=row['merchant_raw'],amount=money(row['amount_minor']),direction=changes.get('direction',row['direction']),transaction_type=changes.get('transaction_type',row['transaction_type']),settlement=changes.get('settlement',row['settlement']),transfer_scope=changes.get('transfer_scope',row['transfer_scope']))
        validate_row(normalized,'manual')
        if 'duplicate_of' in changes:
            target=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(changes['duplicate_of'],)).fetchone()
            if not target or target['transaction_id']==tid or target['duplicate_of'] or not target['active'] or target['amount_minor']!=row['amount_minor'] or target['transaction_type']!=row['transaction_type']:
                raise ValueError('无效的规范重复目标')
            if target['transaction_id'] not in {c['transaction_id'] for c in item['candidates']}:
                raise ValueError('重复目标不在当前候选中')
        if 'raw_id' in changes and changes['raw_id'] not in {s['raw_id'] for s in item['sources']}:
            raise ValueError('来源不在当前活动观测中')
        before={key:row[key] for key in changes}
        for key,value in changes.items():
            con.execute('UPDATE transactions SET '+key+'=? WHERE transaction_id=?',(value,tid))
        decision=con.execute("INSERT INTO decisions(kind,target_id,value_json,created_at) VALUES('review_patch',?,?,?)",(tid,json.dumps(dict(before=before,after=changes),ensure_ascii=False),datetime.now().isoformat())).lastrowid
        con.execute("UPDATE review_items SET status='resolved' WHERE review_id=?",(review_id,))
        match_duplicates(con);match_refunds(con)
        con.execute("UPDATE report_state SET status='pending'")
    return dict(status='resolved',decision_id=decision,transaction_id=tid,artifact_status='pending')


def doctor(vault: Path) -> dict:
    target=vault.expanduser().resolve()
    parent=target
    while not parent.exists() and parent!=parent.parent:
        parent=parent.parent
    return dict(python=sys.version.split()[0],python_supported=sys.version_info>=(3,9),vault=str(target),exists=target.exists(),
        obsidian_marker=(target/'.obsidian').is_dir(),finance_exists=(target/'Finance/Data/finance.sqlite3').is_file(),
        writable=os.access(parent,os.W_OK),skill_resources=(Path(__file__).resolve().parents[1]/'assets/config.example.json').is_file(),
        warning='只读检查；代码仓库不是默认 Vault。无 .obsidian 标记也可由用户明确指定目标。')


def connect_readonly(vault: Path):
    path=database_path(vault)
    if not path.is_file():
        raise ValueError('尚未初始化目标 Vault')
    con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    return con


def read_report(vault: Path, month=None) -> dict:
    import finance_reports
    path=finance_root(vault)/('Months/'+month+'.md' if month else 'Dashboard.md')
    if month:
        month_bounds(month)
    with connect_readonly(vault) as con:
        state=con.execute('SELECT status,error FROM report_state').fetchone()
        analysis_status=None
        if month:
            current,_=finance_reports.current_payload(vault,month,sys.modules[__name__],con)
            context=finance_root(vault)/'Data'/('analysis-context-'+month+'.json')
            old=json.loads(context.read_text()) if context.exists() else {}
            analysis_status=old.get('analysis_status','pending') if old.get('input_fingerprint')==current['input_fingerprint'] else 'stale'
    if not path.is_file():
        raise ValueError('报告尚未生成；请明确请求重建')
    return dict(path=str(path),content=path.read_text(),artifact_status=state['status'],error=state['error'],analysis_status=analysis_status)


def demo(vault: Path) -> dict:
    if vault.exists():
        raise ValueError('演示只接受全新目录，不能混入已有账本')
    vault.mkdir(parents=True,exist_ok=False)
    source=Path(__file__).resolve().parents[1]/'assets/demo-transactions.csv'
    receipt=import_with_receipt(vault,source,coverage='partial')
    build_all_months(vault)
    return dict(demo=True,receipt=receipt,dashboard=str(finance_root(vault)/'Dashboard.md'))


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


purchase_net_amount = matching.purchase_net_amount


def is_debt_funded(transaction: sqlite3.Row) -> bool:
    searchable = "{0} {1} {2}".format(
        transaction["source_platform"],
        transaction["account_name"],
        transaction["payment_method"],
    ).lower()
    return any(keyword in searchable for keyword in ("credit_card", "信用卡", "花呗", "白条"))


@locked
def build_month(vault: Path, month: str) -> Path:
    import finance_reports
    month_bounds(month)
    finance_reports.build_all(vault,sys.modules[__name__],only_month=month)
    return finance_root(vault)/'Months'/(month+'.md')


@locked
def build_all_months(vault: Path) -> List[Path]:
    import finance_reports
    return finance_reports.build_all(vault,sys.modules[__name__])


@locked
def write_analysis(vault: Path, month: str, input_fingerprint: str, findings: str, suggestions: str) -> None:
    import finance_reports
    finance_reports.write_analysis(vault,month,input_fingerprint,findings,suggestions,sys.modules[__name__])


def preserved_marker_content(text, start_marker, end_marker, default):
    if start_marker not in text or end_marker not in text:
        return default
    return text.split(start_marker,1)[1].split(end_marker,1)[0].strip() or default


def purchase_net_amount_from_visible(visible,transaction_id):
    return next((net for row,net in visible if row['transaction_id']==transaction_id),0)


@locked
def build_review_and_quality_pages(vault: Path) -> None:
    """V1 compatibility: rebuild review pages from the current ledger."""
    import finance_reports
    with connect(vault) as con:
        finance_reports.build_review_pages(vault,sys.modules[__name__],con)


@locked
def write_analysis_context(vault,month,visible,total,cash_outflow,refund_inflow,debt_repayment,new_debt,needs_review_count):
    """V1 compatibility. Supplied cached totals never override ledger facts."""
    import finance_reports
    with connect(vault) as con:
        payload,_=finance_reports.current_payload(vault,month,sys.modules[__name__],con)
    old=finance_reports.read_page(finance_root(vault)/'Months'/(month+'.md'))
    payload['analysis_status']=finance_reports.analysis_content(finance_root(vault),month,payload,old)[0]
    output=finance_root(vault)/'Data'/('analysis-context-'+month+'.json')
    atomic_write_text(output,json.dumps(payload,ensure_ascii=False,indent=2))
    return output


@locked
def build_dashboard(vault,month,total,cash_outflow,debt_repayment,new_debt,needs_review_count):
    """V1 compatibility; recompute a consistent Dashboard rather than stale totals."""
    build_month(vault,month)
    return finance_root(vault)/'Dashboard.md'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal finance → Obsidian processor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_command = subparsers.add_parser("init", help="Initialize Finance in an Obsidian vault")
    init_command.add_argument("--vault", required=True, type=Path)

    import_command = subparsers.add_parser("import", help="Import a normalized CSV")
    import_command.add_argument("--vault", required=True, type=Path)
    import_command.add_argument("--file", required=True, type=Path)
    import_command.add_argument('--coverage', choices=('unknown','partial','complete'),default='unknown')
    import_command.add_argument('--source-reference',default='')
    import_command.add_argument('--account-name',default='',help='Explicit native statement account identity')
    import_command.add_argument('--reactivate',action='store_true')
    import_command.add_argument('--json',action='store_true')

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
    debt_command.add_argument("--outstanding", default="")
    debt_command.add_argument("--due", default="")
    debt_command.add_argument("--due-date", default="")

    link_refund_command = subparsers.add_parser(
        "link-refund", help="Manually link an ambiguous refund to a purchase"
    )
    link_refund_command.add_argument("--vault", required=True, type=Path)
    link_refund_command.add_argument("--refund-id", required=True)
    link_refund_command.add_argument("--purchase-id", required=True)
    link_refund_command.add_argument('--snapshot')

    archive_command = subparsers.add_parser(
        "archive", help="Archive a PDF, image, or unsupported original before normalization"
    )
    archive_command.add_argument("--vault", required=True, type=Path)
    archive_command.add_argument("--file", required=True, type=Path)
    archive_command.add_argument("--source", required=True)
    archive_command.add_argument(
        "--coverage", choices=("complete", "partial", "unknown"), default="unknown"
    )
    for name in ('review','reclassify','undo-import','unlink-refund','categorize','undo-decision'):
        command=subparsers.add_parser(name)
        command.add_argument('--vault',required=True,type=Path)
        command.add_argument('--json',action='store_true')
        if name=='undo-import':
            command.add_argument('--batch-id',required=True)
        elif name=='unlink-refund':
            command.add_argument('--refund-id',required=True)
        elif name=='categorize':
            command.add_argument('--transaction-id',required=True)
            command.add_argument('--category',required=True)
            command.add_argument('--scope',choices=('transaction','merchant-future','merchant-all'),default='transaction')
        elif name=='undo-decision':
            command.add_argument('--decision-id',required=True,type=int)
    for name in ('doctor','demo','report','write-analysis','resolve-review'):
        command=subparsers.add_parser(name)
        command.add_argument('--vault',required=True,type=Path)
        command.add_argument('--json',action='store_true')
        if name=='report':
            command.add_argument('--month')
        elif name=='write-analysis':
            command.add_argument('--month',required=True)
            command.add_argument('--fingerprint',required=True)
            command.add_argument('--file',required=True,type=Path,help='JSON with findings and suggestions')
        elif name=='resolve-review':
            command.add_argument('--review-id',required=True)
            command.add_argument('--snapshot',required=True)
            command.add_argument('--changes-json',required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command=='doctor':
            print(json.dumps(doctor(args.vault),ensure_ascii=False,indent=2))
        elif args.command=='demo':
            print(json.dumps(demo(args.vault),ensure_ascii=False,indent=2))
        elif args.command=='report':
            print(json.dumps(read_report(args.vault,args.month),ensure_ascii=False,indent=2))
        elif args.command=='write-analysis':
            content=json.loads(args.file.read_text())
            write_analysis(args.vault,args.month,args.fingerprint,content['findings'],content['suggestions'])
            print(json.dumps(dict(status='current'),ensure_ascii=False))
        elif args.command=='resolve-review':
            print(json.dumps(resolve_review(args.vault,args.review_id,args.snapshot,json.loads(args.changes_json)),ensure_ascii=False))
        elif args.command == "init":
            init_vault(args.vault)
            build_all_months(args.vault)
            print("已初始化: {0}".format(finance_root(args.vault)))
        elif args.command == "import":
            receipt=import_with_receipt(args.vault,args.file,coverage=args.coverage,source_reference=args.source_reference,reactivate=args.reactivate,account_name=args.account_name)
            print(json.dumps(receipt,ensure_ascii=False,indent=2))
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
            link_refund_manually(args.vault, args.refund_id, args.purchase_id,snapshot=args.snapshot)
            print("已人工关联退款: {0} -> {1}".format(args.refund_id, args.purchase_id))
        elif args.command == 'review':
            print(json.dumps(list_reviews(args.vault),ensure_ascii=False,indent=2))
        elif args.command == 'categorize':
            print(json.dumps(set_category(args.vault,args.transaction_id,args.category,args.scope),ensure_ascii=False))
        elif args.command == 'reclassify':
            print(json.dumps(reclassify(args.vault),ensure_ascii=False))
        elif args.command == 'undo-import':
            print(json.dumps(undo_import(args.vault,args.batch_id),ensure_ascii=False))
        elif args.command == 'undo-decision':
            print(json.dumps(undo_decision(args.vault,args.decision_id),ensure_ascii=False))
        elif args.command == 'unlink-refund':
            unlink_refund(args.vault,args.refund_id)
            print(json.dumps(dict(status='unlinked'),ensure_ascii=False))
        elif args.command == "archive":
            output = archive_file(args.vault, args.file, args.source, args.coverage)
            print("已归档原始文件: {0}".format(output))
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print("错误: {0}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
