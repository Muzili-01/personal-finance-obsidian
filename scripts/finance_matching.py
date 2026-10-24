"""Deterministic relationship decisions; manual choices outrank automation."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import difflib
import hashlib
import json
import re


def stable_id(*parts):
    return hashlib.sha256('\x1f'.join(str(p) for p in parts).encode()).hexdigest()[:24]


def merchant_similarity(left, right):
    def normalize(value):
        text = ''.join(c.lower() for c in value if c.isalnum())
        for prefix in ('支付宝', '财付通', '微信支付', '微信'):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text
    left, right = normalize(left), normalize(right)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def review(con, transaction_id, kind, reason, candidates=()):
    details = json.dumps(dict(reason=reason, candidate_purchase_ids=list(candidates)), ensure_ascii=False, sort_keys=True)
    con.execute("INSERT OR REPLACE INTO review_items VALUES(?,?,?,?, 'pending')", (stable_id(transaction_id,kind),transaction_id,kind,details))


def status_kind(row):
    text=row['status'].lower().strip()
    if any(word in text for word in ('失败','关闭','取消','failed','closed','cancelled')):
        return 'failed'
    if any(word in text for word in ('等待','待支付','待付款','处理中','申请','审核','pending','processing')):
        return 'unknown'
    if any(word in text for word in ('成功','已记账','已完成','到账','已支付','交易完成','支付完成','已退款','退款完成','success','completed','posted','settled')):
        return 'success'
    return 'unknown'


def successful(row):
    return status_kind(row)=='success'


OBSERVATION_FIELDS = {
    'transaction_time':'transaction_date','merchant_raw':'merchant','description':'description',
    'direction':'direction','transaction_type':'transaction_type','payment_method':'payment_method',
    'status':'status','merchant_order_id':'merchant_order_id','settlement':'settlement',
    'source_reference':'source_reference','source_locator':'source_locator','transfer_scope':'transfer_scope',
}


def normalized_values(raw):
    defaults=dict(transaction_type='purchase',settlement='unknown',transfer_scope='unknown')
    values={column:raw.get(key,defaults.get(key,'')) or defaults.get(key,'') for column,key in OBSERVATION_FIELDS.items()}
    values['amount_minor']=int((Decimal(str(raw['amount']).replace(',','').replace('¥','').replace('￥',''))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    return values


def sync_observations(con):
    """Derive effective facts from active source support, then apply user patches."""
    con.execute("DELETE FROM review_items WHERE issue_type IN ('source_conflict','manual_inactive')")
    observations=con.execute('''SELECT o.transaction_id,r.raw_id,r.raw_json FROM transaction_observations o
        JOIN raw_records r ON r.raw_id=o.raw_id JOIN import_batches b ON b.batch_id=o.batch_id
        WHERE b.status='active' ORDER BY o.transaction_id,r.raw_id''').fetchall()
    grouped=defaultdict(list)
    for observation in observations:
        raw=json.loads(observation['raw_json'])
        if 'amount' in raw and 'transaction_date' in raw:
            grouped[observation['transaction_id']].append((observation['raw_id'],normalized_values(raw)))
    choices=defaultdict(list)
    for decision in con.execute("SELECT * FROM decisions WHERE kind='review_patch' AND active=1 ORDER BY decision_id"):
        choices[decision['target_id']].append(json.loads(decision['value_json'])['after'])
    significant=('amount_minor','transaction_time','merchant_raw','direction','transaction_type','payment_method','status','merchant_order_id','settlement','transfer_scope')
    for tid,group in grouped.items():
        source_choice=next((patch['raw_id'] for patch in reversed(choices[tid]) if 'raw_id' in patch),None)
        selected=next((pair for pair in group if pair[0]==source_choice),None) if source_choice else group[0]
        if source_choice and not selected:
            review(con,tid,'manual_inactive','人工选择的来源批次已停用；保留决定，未转移到其他来源')
            continue
        signatures={tuple(values[k] for k in significant) for _,values in group}
        if len(signatures)>1 and not source_choice:
            review(con,tid,'source_conflict','同账户交易号的活动来源内容冲突，请选择来源；暂不计入有效合计')
        raw_id,values=selected
        for patch in choices[tid]:
            values.update({key:value for key,value in patch.items() if key in OBSERVATION_FIELDS or key=='amount_minor'})
        values['raw_id']=raw_id
        current=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(tid,)).fetchone()
        if current and (current['merchant_raw']!=values['merchant_raw'] or current['description']!=values['description']) and not current['category_manual']:
            values['category_primary']='待分类'
        changed={key:value for key,value in values.items() if current[key]!=value}
        if changed:
            con.execute('UPDATE transactions SET '+','.join(key+'=?' for key in changed)+' WHERE transaction_id=?',tuple(changed.values())+(tid,))


def prepare_transactions(con):
    sync_observations(con)
    con.execute("DELETE FROM review_items WHERE issue_type IN ('type_unknown','transfer_purpose','status_unknown','refund_state','direction_mismatch','account_unknown','settlement_unknown','duplicate_candidate')")
    con.execute("UPDATE transactions SET excluded_reason=NULL,duplicate_of=NULL WHERE COALESCE(excluded_reason,'')!='identity_duplicate'")
    for row in con.execute('SELECT * FROM transactions WHERE active=1').fetchall():
        tid, kind = row['transaction_id'], row['transaction_type']
        if row['excluded_reason']=='identity_duplicate':
            continue
        conflict=con.execute("SELECT 1 FROM review_items WHERE transaction_id=? AND issue_type IN ('source_conflict','manual_inactive') AND status='pending'",(tid,)).fetchone()
        if conflict:
            con.execute("UPDATE transactions SET excluded_reason='source_conflict' WHERE transaction_id=?",(tid,))
            continue
        if kind=='unknown' or kind=='adjustment':
            review(con,tid,'type_unknown','交易类型/用途需要确认')
        if not row['account_name']:
            review(con,tid,'account_unknown','账户身份未知，禁止跨来源自动匹配')
        if kind=='transfer' and row['transfer_scope']=='unknown':
            # Neutral, explicit own-account asset movements have visible evidence.
            internal = row['direction']=='neutral' and any(x in row['description'] for x in ('余额宝转','零钱通转','本人账户'))
            if not internal:
                review(con,tid,'transfer_purpose','转账用途未知，不能直接当作内部转账')
        expected = {'purchase':'expense','refund':'income','income':'income','investment_income':'income'}
        if kind in expected and row['direction'] != expected[kind]:
            review(con,tid,'direction_mismatch','方向与交易类型不一致')
            con.execute("UPDATE transactions SET excluded_reason='direction_unknown' WHERE transaction_id=?", (tid,))
        state=status_kind(row)
        if state=='failed':
            con.execute("UPDATE transactions SET excluded_reason='failed' WHERE transaction_id=?",(tid,))
            continue
        if state=='unknown':
            review(con,tid,'status_unknown','交易状态待处理/未知，暂不计有效消费/现金流')
            con.execute("UPDATE transactions SET excluded_reason='status_unknown' WHERE transaction_id=?", (tid,))
        if kind=='purchase' and '退款' in row['status']:
            review(con,tid,'refund_state','原购买显示退款状态，需要独立退款事件；未虚构退款')
        if kind in ('purchase','refund','repayment','installment','fee','interest') and row['settlement']=='unknown':
            # A successful refund to an explicit cash balance is settlement
            # evidence. Credit restoration and an absent payment method are not.
            if not cash_settled(row):
                debt_evidence=any(x in (row['source_platform']+' '+row['account_name']+' '+row['payment_method']).lower() for x in ('credit_card','信用卡','花呗','白条'))
                if kind!='purchase' or not debt_evidence:
                    review(con,tid,'settlement_unknown','现金结算未知，未计入已确认现金流')


def cash_settled(row):
    if not successful(row):
        return False
    if row['settlement']=='cash':
        return True
    if row['settlement']=='debt_only':
        return False
    return row['payment_method'] in ('余额','零钱','支付宝余额','微信零钱','储蓄卡','银行卡')


def match_duplicates(con):
    prepare_transactions(con)
    # Explicit duplicate decisions survive every automatic rebuild.
    choices=con.execute("SELECT * FROM decisions WHERE kind='review_patch' AND active=1 ORDER BY decision_id").fetchall()
    for choice in choices:
        patch=json.loads(choice['value_json'])['after']
        target=patch.get('duplicate_of')
        if target:
            valid=con.execute('SELECT active,excluded_reason FROM transactions WHERE transaction_id=?',(target,)).fetchone()
            if valid and valid['active'] and valid['excluded_reason'] is None:
                con.execute("UPDATE transactions SET duplicate_of=?,excluded_reason='duplicate' WHERE transaction_id=?",(target,choice['target_id']))
    rows = con.execute("SELECT * FROM transactions WHERE active=1 AND excluded_reason IS NULL ORDER BY transaction_time,transaction_id").fetchall()
    by_amount = defaultdict(list)
    no_ids = defaultdict(list)
    for row in rows:
        by_amount[(row['transaction_type'],row['amount_minor'])].append(row)
        if not row['platform_transaction_id'] and row['transaction_type']=='purchase':
            no_ids[(row['source_platform'],row['account_name'],row['transaction_time'][:10],row['merchant_raw'],row['amount_minor'])].append(row)
    for group in no_ids.values():
        if len(group)>1:
            for row in group:
                review(con,row['transaction_id'],'duplicate_candidate','无交易号的相似购买，可能高估；未自动抵消', [r['transaction_id'] for r in group if r!=row])
    proposals = {}
    for credit in rows:
        if credit['source_platform']!='credit_card' or credit['transaction_type'] not in ('purchase','refund'):
            continue
        strong, weak = [], []
        for other in by_amount[(credit['transaction_type'],credit['amount_minor'])]:
            if other['source_platform'] not in ('alipay','wechat'):
                continue
            days = abs((datetime.fromisoformat(credit['transaction_time'])-datetime.fromisoformat(other['transaction_time'])).total_seconds()) / 86400
            if days>2 or merchant_similarity(credit['merchant_raw'],other['merchant_raw'])<0.72:
                continue
            weak.append(other)
            tail = credit['account_last4']
            channel = '支付宝' if other['source_platform']=='alipay' else ('微信' if '微信' in credit['merchant_raw'] else '财付通')
            if tail and tail in ''.join(re.findall(r'\d',other['payment_method'])) and channel in credit['merchant_raw']:
                strong.append(other)
        if len(strong)==1:
            proposals[credit['transaction_id']] = strong[0]['transaction_id']
        elif weak:
            review(con,credit['transaction_id'],'duplicate_candidate','支付链路/卡尾号证据不足或候选不唯一；未抵消', [r['transaction_id'] for r in weak])
    target_counts = defaultdict(int)
    for target in proposals.values():
        target_counts[target]+=1
    for credit,target in proposals.items():
        if target_counts[target]==1:
            con.execute("UPDATE transactions SET excluded_reason='duplicate',duplicate_of=? WHERE transaction_id=?", (target,credit))
        else:
            review(con,credit,'duplicate_candidate','同一个支付记录对应多个银行卡候选', [target])


def purchase_net_amount(con, purchase):
    refunded = con.execute('''SELECT COALESCE(SUM(t.amount_minor),0) FROM refund_links l
        JOIN transactions t ON t.transaction_id=l.refund_id
        WHERE l.purchase_id=? AND t.active=1 AND t.excluded_reason IS NULL''', (purchase['transaction_id'],)).fetchone()[0]
    return max(0,purchase['amount_minor']-refunded)


def refund_candidates(con, refund):
    if not refund['account_name']:
        return [], 'account_unknown'
    rows = con.execute("""SELECT * FROM transactions WHERE active=1 AND transaction_type='purchase'
        AND source_platform=? AND account_name=? AND account_last4=? AND transaction_time<=?
        AND (excluded_reason IS NULL OR excluded_reason='duplicate') ORDER BY transaction_time,transaction_id""",
        (refund['source_platform'],refund['account_name'],refund['account_last4'],refund['transaction_time'])).fetchall()
    eligible, strong = {}, {}
    rejected = {r[0] for r in con.execute('SELECT purchase_id FROM refund_rejections WHERE refund_id=?',(refund['transaction_id'],))}
    for row in rows:
        target = row['duplicate_of'] or row['transaction_id']
        if target in rejected or row['amount_minor']<refund['amount_minor']:
            continue
        if row['payment_method'] and refund['payment_method'] and row['payment_method']!=refund['payment_method']:
            continue
        if refund['merchant_order_id'] and row['merchant_order_id']==refund['merchant_order_id']:
            strong[target] = row
        days = (datetime.fromisoformat(refund['transaction_time'])-datetime.fromisoformat(row['transaction_time'])).total_seconds()/86400
        if days<=365 and merchant_similarity(row['merchant_raw'],refund['merchant_raw'])>=0.82:
            eligible[target]=row
    return (list(strong), 'merchant_order_id') if strong else (list(eligible),'unique_merchant_amount')


def match_refunds(con):
    # Recompute automation as a set. Never greedily consume capacity in SQL order.
    con.execute("DELETE FROM refund_links WHERE matched_by!='manual'")
    con.execute("DELETE FROM review_items WHERE issue_type IN ('refund_match','manual_conflict')")
    refunds = con.execute("SELECT * FROM transactions WHERE active=1 AND transaction_type='refund' AND excluded_reason IS NULL ORDER BY transaction_time,transaction_id").fetchall()
    allocations = defaultdict(list)
    for refund in refunds:
        tid = refund['transaction_id']
        manual = con.execute("SELECT * FROM refund_links WHERE refund_id=? AND matched_by='manual'",(tid,)).fetchone()
        candidates,method = refund_candidates(con,refund)
        if manual:
            purchase = con.execute('SELECT * FROM transactions WHERE transaction_id=?',(manual['purchase_id'],)).fetchone()
            if not purchase or not purchase['active'] or purchase['excluded_reason']:
                review(con,tid,'manual_inactive','人工决定的购买来源当前停用，保留历史，不转移关联')
            else:
                decision=con.execute("SELECT value_json FROM decisions WHERE kind='link_refund' AND target_id=? AND active=1 ORDER BY decision_id DESC LIMIT 1",(tid,)).fetchone()
                known=set(json.loads(decision[0]).get('candidates',[])) if decision else set(candidates)
                if set(candidates)-known:
                    review(con,tid,'manual_conflict','新增证据有多个候选；人工决定保持',candidates)
            continue
        paused = con.execute("SELECT 1 FROM decisions WHERE kind IN ('unlink_refund','migration_refund_conflict') AND target_id=? AND active=1",(tid,)).fetchone()
        if paused:
            review(con,tid,'refund_match','人工解除后暂停自动关联，等待明确选择',candidates)
        elif len(candidates)==1:
            allocations[candidates[0]].append((refund,method))
        else:
            review(con,tid,'refund_match','ambiguous' if len(candidates)>1 else 'no_candidate', candidates)
    for pid, group in allocations.items():
        purchase = con.execute('SELECT * FROM transactions WHERE transaction_id=?',(pid,)).fetchone()
        capacity = purchase_net_amount(con,purchase)
        if sum(r['amount_minor'] for r,_ in group)<=capacity:
            for refund,method in group:
                con.execute('INSERT INTO refund_links VALUES(?,?,?,?)',(refund['transaction_id'],pid,method,1.0 if method=='merchant_order_id' else 0.95))
        else:
            for refund,_ in group:
                review(con,refund['transaction_id'],'refund_match','退款合计超过剩余可退金额，未自动分配',[pid])


def list_reviews(con):
    rows = con.execute("""SELECT r.*,t.transaction_time,t.merchant_raw,t.amount_minor,t.source_platform,t.account_name,
        t.raw_id FROM review_items r JOIN transactions t ON t.transaction_id=r.transaction_id
        WHERE r.status='pending' AND t.active=1 ORDER BY t.transaction_time,r.review_id""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        details = json.loads(item.pop('details_json'))
        item['reason'] = details.get('reason','unknown')
        item['candidates'] = [dict(r) for cid in details.get('candidate_purchase_ids',[]) for r in con.execute('SELECT transaction_id,transaction_time,merchant_raw,amount_minor,account_name FROM transactions WHERE transaction_id=?',(cid,))]
        item['sources'] = [dict(r) for r in con.execute('''SELECT r.raw_id,r.source_file,r.source_row,r.raw_json,b.coverage FROM transaction_observations o
            JOIN raw_records r ON r.raw_id=o.raw_id JOIN import_batches b ON b.batch_id=o.batch_id WHERE o.transaction_id=? AND b.status='active' ''',(item['transaction_id'],))]
        item['snapshot'] = stable_id(json.dumps(item,ensure_ascii=False,sort_keys=True))
        result.append(item)
    return result
