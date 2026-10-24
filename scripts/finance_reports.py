"""Derived views. All money comes from the active ledger, never Markdown edits."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from finance_storage import atomic_write_text
import finance_matching as matching

START = '<!-- FINANCE_AUTO_START -->'
END = '<!-- FINANCE_AUTO_END -->'
COVERAGE = {'complete':'完整','partial':'部分','unknown':'未知'}
ANALYSIS = {'pending':'待生成','current':'当前有效','stale':'已过期，待更新','failed':'生成失败'}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def backup(root, path, text, label='page'):
    output=root/'Backups'/(path.stem+'-'+label+'-'+hashlib.sha256(text.encode()).hexdigest()[:16]+path.suffix)
    if not output.exists():
        atomic_write_text(output,text)
    return output


def properties(text):
    """Keep arbitrary foreign YAML chunks byte-for-byte, without interpreting YAML."""
    if not text.startswith('---\n'):
        return {},text
    end=text.find('\n---\n',4)
    if end<0:
        raise ValueError('Properties 边界损坏，保留原页')
    chunks={}
    key=''
    for line in text[4:end+1].splitlines(keepends=True):
        match=re.match(r'^([^\s:#][^:]*):',line)
        if match:
            key=match.group(1)
            if key in chunks:
                raise ValueError('重复 Properties 字段，保留原页: '+key)
            chunks[key]=''
        chunks[key]=chunks.get(key,'')+line
    return chunks,text[end+5:]


def read_page(path):
    return path.read_text(encoding='utf-8') if path.exists() else ''


def managed_page(root, path, body, own, previous=None):
    old=read_page(path) if previous is None else previous
    expected=hashlib.sha256(old.encode()).hexdigest()
    chunks,outer=properties(old)
    generated=START+'\n'+body.rstrip()+'\n'+END
    if START in outer or END in outer:
        if outer.count(START)!=1 or outer.count(END)!=1 or outer.index(START)>outer.index(END):
            backup(root,path,old,'broken')
            raise ValueError('自动区标记损坏，已保留原页；请恢复成对标记后重建: '+str(path))
        outer=outer[:outer.index(START)]+generated+outer[outer.index(END)+len(END):]
    elif old:
        saved=backup(root,path,old,'legacy')
        # Unknown legacy boundaries are never guessed. Preserve the entire old
        # page as explicitly historical text, and preserve a byte-exact backup.
        outer=generated+'\n\n## 用户笔记\n\n<details>\n<summary>历史旧页（仅供追溯，不代表当前结论）</summary>\n\n'+old+'\n</details>\n'
    else:
        outer=generated+'\n\n## 用户笔记\n\n在此记录自己的复盘；重建会保留此区域。\n'
    foreign=''.join(value for key,value in chunks.items() if key not in own)
    front='---\n'+''.join(k+': '+str(v)+'\n' for k,v in own.items())+foreign+'---\n'
    atomic_write_text(path,front+(outer if outer.startswith('\n') else '\n'+outer),expected=expected)


def escaped(value):
    # Bill text is inert data, including HTML/marker-shaped merchant names.
    text=str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('\\','\\\\').replace('\r',' ').replace('\n',' ')
    return re.sub(r'([`*_{}\[\]()#!|])',r'\\\1',text)


def source_links(con, tid, root):
    rows=con.execute('''SELECT r.source_file,r.source_row,r.raw_json,b.source_reference FROM transaction_observations o JOIN raw_records r ON r.raw_id=o.raw_id
        JOIN import_batches b ON b.batch_id=o.batch_id WHERE o.transaction_id=? AND b.status='active' ORDER BY r.source_file,r.source_row''',(tid,)).fetchall()
    result=[]
    for row in rows:
        path=Path(row['source_file'])
        try:
            relative=path.relative_to(root).as_posix()
        except ValueError:
            # Legacy paths might have moved. Keep a visible trace without linking
            # to an arbitrary path outside the authorized Vault.
            relative='Raw/'+path.name
        result.append('[原件行 '+str(row['source_row'])+']('+quote('../'+relative,safe='/')+')')
        raw=json.loads(row['raw_json'])
        reference=raw.get('source_reference') or row['source_reference']
        if reference:
            original=Path(reference)
            if not original.is_absolute():
                original=root/original
            try:
                original=original.resolve()
                relative_original=original.relative_to((root/'Raw').resolve()).as_posix()
                if original.is_file():
                    label='归档来源 '+escaped(raw.get('source_locator',''))
                    result.append('['+label+']('+quote('../Raw/'+relative_original,safe='/')+')')
            except (ValueError,OSError):
                # A source reference is never permission to follow links out of
                # this Vault's Raw archive or load remote content.
                pass

    return ' · '.join(result) or '来源当前停用'


def month_payload(vault, month, f, con):
    start,end=f.month_bounds(month)
    transactions=con.execute('SELECT * FROM transactions WHERE active=1 AND transaction_time>=? AND transaction_time<? ORDER BY transaction_time,transaction_id',(start,end)).fetchall()
    visible=[]
    categories=defaultdict(int);merchants=defaultdict(int)
    metrics=dict(total_consumption=0,cash_outflow=0,refund_inflow=0,debt_repayment=0,new_debt=0,financial_costs=0,income=0,asset_conversion=0)
    settlement_unknown=0
    for row in transactions:
        if row['excluded_reason']:
            continue
        amount=row['amount_minor'];kind=row['transaction_type']
        if kind=='purchase':
            net=f.purchase_net_amount(con,row)
            if net>0:
                visible.append((row,net));categories[row['category_primary']]+=net;merchants[row['merchant_raw']]+=net
                metrics['total_consumption']+=net
            if f.is_debt_funded(row):
                metrics['new_debt']+=amount  # A later refund is not historical debt repayment.
            elif row['direction']=='expense' and matching.cash_settled(row):
                metrics['cash_outflow']+=amount
            elif row['settlement']=='unknown':
                settlement_unknown+=1
        elif kind in ('repayment','installment'):
            metrics['debt_repayment']+=amount
            if row['direction']=='expense' and matching.cash_settled(row):
                metrics['cash_outflow']+=amount
        elif kind in ('fee','interest'):
            metrics['financial_costs']+=amount
            if row['direction']=='expense' and matching.cash_settled(row):
                metrics['cash_outflow']+=amount
        elif kind=='refund':
            if matching.cash_settled(row):
                metrics['refund_inflow']+=amount
            elif row['settlement']=='unknown':
                settlement_unknown+=1
        elif kind in ('income','investment_income'):
            metrics['income']+=amount
        elif kind in ('investment_buy','investment_sell','topup','withdrawal','transfer','cash_withdrawal'):
            metrics['asset_conversion']+=amount
    reviews=matching.list_reviews(con)
    reviews=[r for r in reviews if start<=r['transaction_time']<end]
    review_transactions={r['transaction_id']:r['amount_minor'] for r in reviews}
    observations=con.execute('''SELECT DISTINCT t.source_platform,t.account_name,b.coverage,b.source_reference,
        t.transaction_time FROM transactions t JOIN transaction_observations o ON o.transaction_id=t.transaction_id
        JOIN import_batches b ON b.batch_id=o.batch_id WHERE b.status='active' AND t.transaction_time>=? AND t.transaction_time<?''',(start,end)).fetchall()
    statuses={r['coverage'] for r in observations}
    coverage_status='partial' if 'partial' in statuses else ('unknown' if not statuses or 'unknown' in statuses else 'complete')
    accounts=sorted({(r['source_platform'],r['account_name']) for r in observations})
    times=[r['transaction_time'] for r in observations]
    coverage=dict(status=coverage_status,accounts=[dict(platform=p,account=a) for p,a in accounts],start=min(times) if times else None,end=max(times) if times else None)
    payload=dict(month=month, metrics={k:f.money(v) for k,v in metrics.items()},
        categories=[dict(name=k,amount=f.money(v),share=f.money((v*10000)//metrics['total_consumption'])) for k,v in sorted(categories.items(),key=lambda item:(-item[1],item[0]))],
        top_merchants=[dict(name=k,amount=f.money(v)) for k,v in sorted(merchants.items(),key=lambda item:(-item[1],item[0]))[:10]],
        small_purchases=dict(threshold='50.00',count=sum(1 for _,net in visible if net<5000),amount=f.money(sum(net for _,net in visible if net<5000))),
        purchase_count=len(visible),needs_review_count=len(review_transactions),review_issue_count=len(reviews),review_amount=f.money(sum(review_transactions.values())),
        unclassified_count=sum(1 for row,_ in visible if row['category_primary']=='待分类'),
        settlement_unknown_count=settlement_unknown,coverage=coverage,reconciliation_status='unreconciled',
        sources=sorted({r['source_reference'] for r in observations if r['source_reference']}),
        analysis_guardrail='仅分析已识别范围；全额退款已移出消费输入；无可比完整覆盖不声称趋势。')
    fingerprint_input=dict(payload)
    fingerprint_input.pop('sources')
    fingerprint_input['purchases']=[dict(transaction_id=r['transaction_id'],time=r['transaction_time'],merchant=r['merchant_raw'],category=r['category_primary'],net_minor=net) for r,net in visible]
    fingerprint_input['reviews']=[dict(transaction_id=r['transaction_id'],issue_type=r['issue_type'],reason=r['reason'],candidates=sorted(c['transaction_id'] for c in r['candidates'])) for r in reviews]
    payload['input_fingerprint']=digest(fingerprint_input)
    return payload,visible


def apply_comparison(payload, previous, f):
    adjacent=bool(previous and f.month_bounds(previous['month'])[1]==f.month_bounds(payload['month'])[0])
    same=bool(adjacent and payload['coverage']['status']=='complete' and previous['coverage']['status']=='complete' and payload['coverage']['accounts']==previous['coverage']['accounts'])
    payload['comparison']=dict(comparable=same)
    if same:
        payload['comparison']['delta']=f.money(f.amount_to_minor(payload['metrics']['total_consumption'])-f.amount_to_minor(previous['metrics']['total_consumption']))
        payload['comparison']['previous_month']=previous['month']
        payload['comparison']['previous_metrics']=previous['metrics']
    payload['input_fingerprint']=digest(dict(base=payload['input_fingerprint'],comparison=payload['comparison']))
    return payload


def current_payload(vault,month,f,con):
    payload,visible=month_payload(vault,month,f,con)
    prior=con.execute('SELECT MAX(substr(transaction_time,1,7)) FROM transactions WHERE transaction_time<?',(month+'-01',)).fetchone()[0]
    previous=month_payload(vault,prior,f,con)[0] if prior else None
    apply_comparison(payload,previous,f)
    return payload,visible


def marker(text,name,default=''):
    start='<!-- AI_'+name+'_START -->';end='<!-- AI_'+name+'_END -->'
    if text.count(start)==1 and text.count(end)==1 and text.index(start)<text.index(end):
        return text.split(start,1)[1].split(end,1)[0].strip()
    return default


def analysis_content(root,month,payload,old_page):
    analysis_path=root/'Data'/('analysis-'+month+'.json')
    context_path=root/'Data'/('analysis-context-'+month+'.json')
    previous=json.loads(context_path.read_text()) if context_path.exists() else {}
    saved=json.loads(analysis_path.read_text()) if analysis_path.exists() else None
    if saved and saved.get('input_fingerprint')==payload['input_fingerprint']:
        return 'current',saved['findings'],saved['suggestions']
    # Compatibility: V1.1 reports manually edited in their AI markers can still
    # retain those edits when the computed input is byte-for-byte equivalent.
    legacy_analysis=marker(old_page,'ANALYSIS')
    if not saved and previous.get('input_fingerprint')==payload['input_fingerprint'] and legacy_analysis and not legacy_analysis.startswith(('待 Agent','分析')):
        return 'current',legacy_analysis,marker(old_page,'SUGGESTIONS')
    stale=bool(saved or (previous and previous.get('input_fingerprint')!=payload['input_fingerprint'] and legacy_analysis and not legacy_analysis.startswith(('待 Agent','分析'))))
    if stale and old_page:
        backup(root,root/'Months'/(month+'.md'),old_page,'analysis')
    return ('stale' if stale else 'pending'),'分析待更新：请依据当前分析输入生成。','待 Agent 基于可验证指标生成不超过三条建议。'


def month_body(payload,visible,con,root,f,findings,suggestions):
    month=payload['month'];metrics=payload['metrics']
    lines=['# '+month+' 财务报告','','## 本月概览','',
        '- 来源覆盖：'+COVERAGE[payload['coverage']['status']]+'；金额核对：未核对（未提供同口径来源汇总）',
        '- 已覆盖账户：'+('、'.join(escaped(a['platform']+'/'+a['account']) for a in payload['coverage']['accounts']) or '未知'),
        '- 覆盖日期：'+str(payload['coverage']['start'] or '未知')+' 至 '+str(payload['coverage']['end'] or '未知'),
        '- 已识别净消费：¥'+metrics['total_consumption'], '- 已确认现金支出：¥'+metrics['cash_outflow'],
        '- 退款实际流入：¥'+metrics['refund_inflow']+'；结算未知：'+str(payload['settlement_unknown_count'])+' 笔',
        '- 债务还款：¥'+metrics['debt_repayment'], '- 新增借贷消费：¥'+metrics['new_debt']+'（不是月末余额）',
        '- 金融成本：¥'+metrics['financial_costs'],
        '- 待确认：'+str(payload['needs_review_count'])+' 笔，涉及 ¥'+payload['review_amount']+'；待分类：'+str(payload['unclassified_count'])+' 笔',
        '- 分析状态：'+ANALYSIS[payload['analysis_status']]+'；不完整或待确认结果均为暂定。','',
        '现金流按已覆盖账户与结算证据计；还款、资产转换不新增消费。退款按购买月抵扣，现金到账按退款实际结算月计。','',
        '## 消费明细','','| 日期 | 商户 | 分类 | 净消费 | 查证 |','| --- | --- | --- | ---: | --- |']
    for row,net in visible:
        lines.append('| '+row['transaction_time'][:10]+' | '+escaped(row['merchant_raw'])+' | '+escaped(row['category_primary'])+' | ¥'+f.money(net)+' | `'+row['transaction_id']+'` '+source_links(con,row['transaction_id'],root)+' |')
    if not visible:
        lines.append('| — | 此范围没有净消费记录；覆盖以数据状态为准 | — | ¥0.00 | — |')
    lines+=['','## 消费分类','','| 分类 | 金额 | 占比 |','| --- | ---: | ---: |']
    lines+=['| '+escaped(c['name'])+' | ¥'+c['amount']+' | '+c['share']+'% |' for c in payload['categories']]
    lines+=['','## 主要商户','','| 商户 | 净消费 |','| --- | ---: |']
    lines+=['| '+escaped(c['name'])+' | ¥'+c['amount']+' |' for c in payload['top_merchants']]
    lines+=['','## 消费习惯分析','','<!-- AI_ANALYSIS_START -->',findings,'<!-- AI_ANALYSIS_END -->','','## 下月建议','','<!-- AI_SUGGESTIONS_START -->',suggestions,'<!-- AI_SUGGESTIONS_END -->','','## 数据质量','',
            '- 报告由原始记录派生；原始记录未删除。', '- [[../Reviews/待确认交易|待确认交易]] · [[../Reviews/数据质量报告|数据质量报告]] · [[../Reviews/退款核对|退款核对]]','- 更新时间：'+datetime.now().astimezone().isoformat(timespec='seconds')]
    return '\n'.join(lines)


def build_review_pages(vault,f,con):
    root=f.finance_root(vault);reviews=matching.list_reviews(con)
    lines=['# 待确认交易','','待确认交易：'+str(len(reviews))+'。请用自然语言选择；暂不确定可保留。','']
    for review in reviews:
        lines+=['## '+review['transaction_time'][:10]+' · '+escaped(review['merchant_raw'])+' · ¥'+f.money(review['amount_minor']),'',
            '- 问题：'+review['issue_type']+'；'+escaped(review['reason']),'- 查证：'+source_links(con,review['transaction_id'],root),'- 候选消费：']
        for i,c in enumerate(review['candidates'],1):
            lines.append('  - '+str(i)+'：'+c['transaction_time']+' · '+escaped(c['merchant_raw'])+' · '+escaped(c['account_name'])+' · ¥'+f.money(c['amount_minor']))
        if not review['candidates']:
            lines.append('  - 无可确认候选；需要补充证据。')
        lines+=['','内部复核标识：`'+review['review_id']+'`；请让 Agent 重新读取候选后操作。','']
    managed_page(root,root/'Reviews/待确认交易.md','\n'.join(lines),dict(type='finance-review',pending_count=len(reviews)))
    counts={table:con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('source_files','raw_records','transactions','refund_links')}
    quality=['# 数据质量报告','', '- 原始文件：'+str(counts['source_files']),'- 原始记录：'+str(counts['raw_records']),'- 标准化交易：'+str(counts['transactions']),'- 已关联退款：'+str(counts['refund_links']),'- 待确认交易：'+str(len(reviews)),
        '', '金额核对：未核对。没有同口径来源汇总时不宣称已完成金额勾稽。截图默认部分覆盖；疑似重复保留可能高估的暂定值。',
        '待确认金额按交易去重，不等于最终修正额；数据库及原件、备份均为未加密本地文件。']
    managed_page(root,root/'Reviews/数据质量报告.md','\n'.join(quality),dict(type='finance-data-quality'))
    refunds=con.execute("SELECT t.*,l.purchase_id,l.matched_by FROM transactions t LEFT JOIN refund_links l ON l.refund_id=t.transaction_id WHERE t.active=1 AND t.transaction_type='refund' ORDER BY t.transaction_time,t.transaction_id").fetchall()
    lines=['# 退款核对','','全额退款仅在本页查证，不进入消费习惯输入。','','| 退款 | 金额 | 原购买 | 关联依据 | 结算 | 原始依据 |','| --- | ---: | --- | --- | --- | --- |']
    for row in refunds:
        purchase=con.execute('SELECT * FROM transactions WHERE transaction_id=?',(row['purchase_id'],)).fetchone() if row['purchase_id'] else None
        purchase_text=(purchase['transaction_time']+' '+escaped(purchase['merchant_raw'])+' 净额 ¥'+f.money(f.purchase_net_amount(con,purchase))+' `'+purchase['transaction_id']+'` '+source_links(con,purchase['transaction_id'],root)) if purchase else '待确认'
        settlement='现金到账' if matching.cash_settled(row) else ('仅冲减债务' if row['settlement']=='debt_only' else '结算未知')
        lines.append('| '+row['transaction_time']+' '+escaped(row['merchant_raw'])+' | ¥'+f.money(row['amount_minor'])+' | '+purchase_text+' | '+str(row['matched_by'] or '未关联')+' | '+settlement+' | '+source_links(con,row['transaction_id'],root)+' |')
    managed_page(root,root/'Reviews/退款核对.md','\n'.join(lines),dict(type='finance-refunds'))


def build_dashboard(vault,payloads,f,con):
    root=f.finance_root(vault)
    active_months=[p for p in payloads if p['coverage']['accounts']]
    latest=active_months[-1] if active_months else None
    all_reviews=matching.list_reviews(con)
    all_review_amount={r['transaction_id']:r['amount_minor'] for r in all_reviews}
    lines=['# 个人财务 Dashboard','', '- 全部月份待确认：'+str(len(all_review_amount))+' 笔，涉及 ¥'+f.money(sum(all_review_amount.values())),'']
    if latest:
        lines+=['## 最新数据：'+latest['month'],'','- 来源覆盖：'+COVERAGE[latest['coverage']['status']]+'；金额核对：未核对',
            '- 已覆盖账户：'+('、'.join(escaped(a['platform']+'/'+a['account']) for a in latest['coverage']['accounts']) or '未知'),'- 覆盖日期：'+str(latest['coverage']['start'])+' 至 '+str(latest['coverage']['end']), '- 待确认：'+str(latest['needs_review_count'])+' 笔，涉及 ¥'+latest['review_amount'], '- 分析：'+ANALYSIS[latest['analysis_status']],
            '', '| 已识别净消费 | 已确认现金支出 | 退款实际流入 | 债务还款 | 新增借贷消费 |','| ---: | ---: | ---: | ---: | ---: |',
            '| '+' | '.join('¥'+latest['metrics'][key] for key in ('total_consumption','cash_outflow','refund_inflow','debt_repayment','new_debt'))+' |',
            '', '仅为已覆盖账户与已识别范围；未知结算不作为现金到账。','','## 消费构成','', '| 分类 | 净额 | 占比 |','| --- | ---: | ---: |']
        lines+=['| '+escaped(c['name'])+' | ¥'+c['amount']+' | '+c['share']+'% |' for c in latest['categories']]
        if latest['analysis_status']=='current':
            page=read_page(root/'Months'/(latest['month']+'.md'))
            lines+=['','## 本期发现与建议','',marker(page,'ANALYSIS'),'',marker(page,'SUGGESTIONS'),'', '详见 [[Months/'+latest['month']+'|月报]]，分析当前有效。']
        else:
            lines+=['','需要处理：分析待更新；先核对待确认及缺失覆盖。']
    else:
        lines+=['尚未导入账单。消费与现金流：未知；请提供指定来源的账单。','']
    debts=con.execute('''SELECT d.* FROM debt_snapshots d JOIN (SELECT account_name,MAX(as_of_date) latest FROM debt_snapshots GROUP BY account_name) x
        ON d.account_name=x.account_name AND d.as_of_date=x.latest ORDER BY d.account_name''').fetchall()
    lines+=['','## 债务与待还款','','快照仅截至所示日期，不表示今天余额。','','| 账户 | 截止日期 | 剩余待还 | 本期应还 | 最近还款日 |','| --- | --- | ---: | ---: | --- |']
    display=lambda x:'未知' if x is None else '¥'+f.money_display(x)
    for debt in debts:
        lines.append('| '+escaped(debt['account_name'])+' | '+debt['as_of_date']+' | '+display(debt['outstanding_minor'])+' | '+display(debt['due_minor'])+' | '+(debt['due_date'] or '未知')+' |')
        account_path=root/'Accounts'/(f.stable_id(debt['account_name'])+'.md')
        managed_page(root,account_path,'# '+escaped(debt['account_name'])+'\n\n截止：'+debt['as_of_date']+'\n\n剩余待还：'+display(debt['outstanding_minor'])+'；本期应还：'+display(debt['due_minor']),dict(type='finance-account'))
    if not debts:
        lines+=['| 尚未提供 | 未知 | 未知 | 未知 | 未知 |']
    lines+=['','## 月度趋势与历史入口','','| 月报 | 净消费 | 覆盖 | 比较结论 |','| --- | ---: | --- | --- |']
    for p in payloads:
        comparison='不可比：覆盖/账户或期间证据不足'
        if p['comparison']['comparable']:
            comparison='同覆盖净消费变动 ¥'+p['comparison']['delta']
        lines.append('| [[Months/'+p['month']+'|'+p['month']+']] | ¥'+p['metrics']['total_consumption']+' | '+COVERAGE[p['coverage']['status']]+' | '+comparison+' |')
    lines+=['','## 快速入口','', '- [[Reviews/待确认交易|待确认交易]]','- [[Reviews/数据质量报告|数据质量报告]]','- [[Reviews/退款核对|退款核对]]','- [[Imports/导入记录|导入与撤销记录]]','', '更新时间：'+datetime.now().astimezone().isoformat(timespec='seconds')]
    managed_page(root,root/'Dashboard.md','\n'.join(lines),dict(type='finance-dashboard',current_month=latest['month'] if latest else 'unknown'))
    batches=con.execute('SELECT * FROM import_batches ORDER BY created_at,batch_id').fetchall()
    index=['# 导入记录','', '| 批次 | 文件 | 状态 | 覆盖 | 时间 |','| --- | --- | --- | --- | --- |']
    for b in batches:
        receipt=json.loads(b['receipt_json'])
        receipt.update(batch_id=b['batch_id'],source_file=b['source_file'],source_name=receipt.get('source_name',Path(b['source_file']).name),created_at=b['created_at'],current_batch_status=b['status'])
        atomic_write_text(root/'Imports'/(b['batch_id']+'.json'),json.dumps(receipt,ensure_ascii=False,indent=2))
        index.append('| ['+b['batch_id']+']('+b['batch_id']+'.json) | '+escaped(Path(b['source_file']).name)+' | '+b['status']+' | '+COVERAGE.get(b['coverage'],'未知')+' | '+b['created_at']+' |')
    for action in con.execute("SELECT decision_id,value_json FROM decisions WHERE kind='undo_import'"):
        atomic_write_text(root/'Imports'/('undo-'+str(action['decision_id'])+'.json'),json.dumps(json.loads(action['value_json']),ensure_ascii=False,indent=2))
    managed_page(root,root/'Imports/导入记录.md','\n'.join(index),dict(type='finance-imports'))


def build_all(vault,f,only_month=None):
    f.init_vault(vault)
    root=f.finance_root(vault)
    try:
        with f.connect(vault) as con:
            # Matching is deterministic, and rerunning it also surfaces new
            # conflicts without modifying manual selections.
            f.match_duplicates(con);f.match_refunds(con)
        with f.connect(vault) as con:
            months=[r[0] for r in con.execute('SELECT DISTINCT substr(transaction_time,1,7) month FROM transactions ORDER BY month')]
            if only_month and only_month not in months:
                months.append(only_month);months.sort()
            payloads=[];paths=[];previous=None
            for month in months:
                payload,visible=month_payload(vault,month,f,con)
                apply_comparison(payload,previous,f)
                path=root/'Months'/(month+'.md');old=read_page(path)
                status,findings,suggestions=analysis_content(root,month,payload,old)
                payload['analysis_status']=status
                payload['updated_at']=datetime.now().astimezone().isoformat(timespec='seconds')
                own=dict(type='finance-month',month=month,**payload['metrics'],needs_review_count=payload['needs_review_count'],coverage=payload['coverage']['status'],analysis_status=status)
                managed_page(root,path,month_body(payload,visible,con,root,f,findings,suggestions),own,previous=old)
                atomic_write_text(root/'Data'/('analysis-context-'+month+'.json'),json.dumps(payload,ensure_ascii=False,indent=2))
                payloads.append(payload);paths.append(path);previous=payload
            build_review_pages(vault,f,con)
            build_dashboard(vault,payloads,f,con)
            con.execute("UPDATE report_state SET status='current',error=''")
        return paths
    except (OSError,ValueError) as exc:
        with f.connect(vault) as con:
            con.execute("UPDATE report_state SET status='failed',error=?",(str(exc),))
        raise


def write_analysis(vault,month,input_fingerprint,findings,suggestions,f):
    with f.connect(vault) as con:
        payload,_=current_payload(vault,month,f,con)
    if input_fingerprint!=payload['input_fingerprint']:
        raise ValueError('分析输入已过期/发生变化，请重新读取分析上下文')
    if any(mark in findings+suggestions for mark in ('<!-- FINANCE_','<!-- AI_')):
        raise ValueError('分析正文不能包含管理标记')
    path=f.finance_root(vault)/'Data'/('analysis-'+month+'.json')
    if path.exists():
        backup(f.finance_root(vault),path,path.read_text(),'analysis')
    atomic_write_text(path,json.dumps(dict(input_fingerprint=input_fingerprint,findings=findings,suggestions=suggestions),ensure_ascii=False,indent=2))
    build_all(vault,f)
