"""Durable ordinary-send reservation; force never releases an unknown effect."""
import json
import secrets
import time
from mcp_rich_publication import ledger


def database(path):
    db=ledger({'IDEMPOTENCY_FILE':path})
    db.execute('CREATE TABLE IF NOT EXISTS ordinary_dispatches '
               '(hash TEXT PRIMARY KEY, nonce TEXT, state TEXT, created REAL, payload TEXT, receipt TEXT)')
    return db


def reserve(path, action_hash, payload, force=False, window=86400):
    db=database(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM ordinary_dispatches WHERE hash=?',(action_hash,)).fetchone()
            if row and row['state'] in ('pending','unknown'):
                return {'allowed':False,'error_code':'prior_send_effect_unknown',
                        'receipt':json.loads(row['receipt']) if row['receipt'] else None,
                        'automatic_retry_allowed':False}
            if row and row['state']=='sent' and not force and window>0 and time.time()-row['created']<window:
                return {'allowed':False,'error_code':'duplicate_send_reserved','automatic_retry_allowed':False}
            nonce=secrets.token_hex(16)
            db.execute("INSERT OR REPLACE INTO ordinary_dispatches VALUES (?,?, 'pending',?,?,NULL)",
                       (action_hash,nonce,time.time(),json.dumps(payload,ensure_ascii=False)))
        return {'allowed':True,'nonce':nonce}
    finally:db.close()


def finish(path, action_hash, nonce, state, receipt):
    if state not in ('sent','unknown','rejected'):raise ValueError('invalid_dispatch_state')
    db=database(path)
    try:
        with db:
            changed=db.execute('UPDATE ordinary_dispatches SET state=?,receipt=? '
                               'WHERE hash=? AND nonce=?',
                               (state,json.dumps(receipt,ensure_ascii=False),action_hash,nonce)).rowcount
            if changed!=1:raise ValueError('dispatch_reservation_changed')
    finally:db.close()
