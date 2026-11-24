from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from ..db import connect
from ..resident_operations import audit, get
from .budgets import committed
from .offers import BASE, locked
from .rules import LOCAL, first_invoice, today


def board():
    with connect() as conn:
        opts=conn.execute(BASE+" WHERE o.status IN ('issued','signed') ORDER BY o.id DESC").fetchall()
        contracts={row['offer_id']:row for row in conn.execute('SELECT * FROM renewal_contract').fetchall()}
        pending={row['offer_id']:row for row in
                 conn.execute("SELECT * FROM renewal_contract_pending WHERE status='pending'").fetchall()}
        invoices={}
        for row in conn.execute('SELECT * FROM renewal_invoice_check ORDER BY checked_at DESC,id DESC').fetchall():
            invoices.setdefault(row['offer_id'],[]).append(row)
        for row in opts:
            row['contract']=contracts.get(row['id'])
            row['pending']=pending.get(row['id'])
            row['invoices']=invoices.get(row['id'],[])
    return {'offers':opts,'today':today()}


def book(conn,offer,contract_ref,signed_on,user):
    period,expected=first_invoice(offer['lease_start'],offer['list_cents']-offer['monthly_discount_cents'])
    try:
        row=conn.execute('''INSERT INTO renewal_contract(offer_id,contract_ref,signed_on,recorded_by,first_period_end,first_expected_cents)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING *''',(offer['id'],contract_ref,signed_on,user['id'],period,expected)).fetchone()
    except UniqueViolation as exc:
        raise HTTPException(409,'合同凭证已用于其他报价') from exc
    conn.execute("UPDATE renewal_offer SET status='signed',acted_at=now() WHERE id=%s",(offer['id'],))
    audit(conn,user,'renewal_contract_recorded','renewal_offer',offer['id'],contract_ref)
    return row


def budget_allows(conn,offer):
    budget=get(conn,'SELECT cap_cents FROM retention_budget WHERE id=%s FOR UPDATE',(offer['budget_id'],),'片区让利预算不存在')
    return committed(conn,offer['budget_id'])+offer['total_discount_cents']<=budget['cap_cents']


def sign(offer_id,payload,user):
    with connect() as conn:
        offer=locked(conn,offer_id)
        if offer['status']!='issued':
            raise HTTPException(409,'只有已发出且未签约报价可以登记合同')
        issued_day=(offer['acted_at'] or offer['created_at']).astimezone(LOCAL).date()
        if not (issued_day<=payload.signed_on<=min(today(),offer['valid_through'])):
            raise HTTPException(422,'签约日期须在报价发出至有效期之间且不能是未来')
        if offer['valid_through']>=today():
            return book(conn,offer,payload.contract_ref,payload.signed_on,user)
        if conn.execute("SELECT 1 FROM renewal_contract_pending WHERE offer_id=%s AND status='pending'",(offer_id,)).fetchone():
            raise HTTPException(409,'该报价已有待处理的签约凭证，请按原凭证重试补签')
        if budget_allows(conn,offer):
            return book(conn,offer,payload.contract_ref,payload.signed_on,user)
        try:
            row=conn.execute('''INSERT INTO renewal_contract_pending(offer_id,contract_ref,signed_on,recorded_by)
                VALUES(%s,%s,%s,%s) RETURNING *''',(offer_id,payload.contract_ref,payload.signed_on,user['id'])).fetchone()
        except UniqueViolation as exc:
            raise HTTPException(409,'合同凭证已用于其他报价') from exc
        audit(conn,user,'renewal_contract_pending','renewal_offer',offer_id,payload.contract_ref)
    return {'message':'片区额度不足，签约凭证留待处理，暂不出首账单','voucher':row}


def retry(offer_id,user):
    with connect() as conn:
        offer=locked(conn,offer_id)
        if offer['status']!='issued':
            raise HTTPException(409,'报价已签约或关闭，无需重试')
        voucher=get(conn,"SELECT * FROM renewal_contract_pending WHERE offer_id=%s AND status='pending'",
                    (offer_id,),'没有待处理的签约凭证')
        if not budget_allows(conn,offer):
            raise HTTPException(409,'片区额度仍不足，签约凭证继续留待处理')
        row=book(conn,offer,voucher['contract_ref'],voucher['signed_on'],user)
        conn.execute("UPDATE renewal_contract_pending SET status='booked',booked_at=now() WHERE offer_id=%s",(offer_id,))
        audit(conn,user,'renewal_contract_retried','renewal_offer',offer_id,voucher['contract_ref'])
    return row


def check_invoice(offer_id,payload,user):
    with connect() as conn:
        offer=locked(conn,offer_id)
        if offer['status']!='signed':
            raise HTTPException(409,'先登记签约凭证再核对首张账单')
        contract=get(conn,'SELECT * FROM renewal_contract WHERE offer_id=%s',(offer_id,),'续租合同不存在')
        if conn.execute("SELECT 1 FROM renewal_invoice_check WHERE offer_id=%s AND status='matched'",(offer_id,)).fetchone():
            raise HTTPException(409,'首账单已核对')
        try:
            row=conn.execute('''INSERT INTO renewal_invoice_check(offer_id,invoice_ref,invoiced_cents,expected_cents,note,checked_by)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *''',
                (offer_id,payload.invoice_ref,payload.invoiced_cents,contract['first_expected_cents'],payload.note,user['id'])).fetchone()
        except UniqueViolation as exc:
            raise HTTPException(409,'账单号已登记') from exc
        audit(conn,user,'renewal_first_invoice_checked','renewal_offer',offer_id,payload.invoice_ref+' '+row['status'])
    return row
