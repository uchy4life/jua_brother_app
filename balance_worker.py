"""Reconcile and collect deposit balances."""
import logging
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import stripe
import psycopg

import app

log = logging.getLogger(__name__)
MIDNIGHT_CONSENT_TERMS_VERSIONS = frozenset({'2026-11-24'})
POSTGRES_WORKER_LOCK = 581234902


def due(start_date, now, midnight=False):
    local = now.astimezone(ZoneInfo('Europe/London'))
    return local.date().isoformat() > start_date or (
        local.date().isoformat() == start_date and (midnight or local.hour >= 9))


def mark_paid(booking_id, intent_id, amount):
    with app.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('''SELECT b.subtotal_pence,b.paid_pence,p.status,p.intent_id
            FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
        if (not row or row['status'] not in ('confirming', 'processing') or row['intent_id'] != intent_id
                or amount != row['subtotal_pence'] - row['paid_pence']):
            if row and row['status'] == 'paid' and row['intent_id'] == intent_id:
                return
            raise ValueError('Balance payment mismatch')
        con.execute("UPDATE bookings SET paid_pence=subtotal_pence WHERE id=?", (booking_id,))
        con.execute("UPDATE balance_payments SET status='paid',last_error=NULL WHERE booking_id=?", (booking_id,))


def process_booking(booking_id, client):
    with app.db() as con:
        row = con.execute('''SELECT b.*,p.consent_at,p.status AS balance_status,p.intent_id,
            p.created_at AS balance_created FROM bookings b JOIN balance_payments p ON p.booking_id=b.id
            WHERE b.id=?''', (booking_id,)).fetchone()
    if not row or row['balance_status'] not in ('scheduled', 'creating', 'confirming', 'processing'):
        return
    amount = row['subtotal_pence'] - row['paid_pence']
    if amount <= 0:
        return
    if not row['consent_at']:
        with app.db() as con:
            con.execute("""UPDATE balance_payments SET status='needs_manual',last_error='No saved-payment consent'
                WHERE booking_id=? AND status IN ('scheduled','creating','confirming','processing')""", (booking_id,))
        return
    if row['balance_status'] == 'scheduled':
        with app.db() as con:
            claimed = con.execute("UPDATE balance_payments SET status='creating',created_at=? WHERE booking_id=? AND status='scheduled'",
                                  (datetime.now(timezone.utc).isoformat(), booking_id))
            if claimed.rowcount != 1:
                return
    if row['balance_status'] in ('scheduled', 'creating'):
        started = datetime.fromisoformat(row['balance_created']) if row['balance_status'] == 'creating' else datetime.now(timezone.utc)
        if datetime.now(timezone.utc) - started > timedelta(hours=20):
            with app.db() as con:
                con.execute("""UPDATE balance_payments SET status='needs_review',last_error='Creation outcome unresolved'
                    WHERE booking_id=? AND status='creating'""", (booking_id,))
            return
        session = client.v1.checkout.sessions.retrieve(row['stripe_session_id'])
        if not session.customer or not session.payment_intent:
            missing_payment_method(booking_id, row['balance_status'], 'Deposit Checkout missing customer or payment intent')
            return
        deposit = client.v1.payment_intents.retrieve(session.payment_intent, {'expand': ['latest_charge']})
        if deposit.status != 'succeeded' or not deposit.payment_method:
            missing_payment_method(booking_id, row['balance_status'], 'Deposit payment method unavailable')
            return
        charge = getattr(deposit, 'latest_charge', None)
        if (not isinstance(charge, Mapping) or not isinstance(charge.get('amount_refunded'), int)
                or not isinstance(charge.get('disputed'), bool)
                or charge['amount_refunded'] > 0 or charge['disputed']):
            with app.db() as con:
                con.execute("""UPDATE balance_payments SET status='needs_review',
                    last_error='Deposit refund, dispute, or charge details require review'
                    WHERE booking_id=? AND status='creating'""", (booking_id,))
            return
        intent = client.v1.payment_intents.create({
            'amount': amount, 'currency': 'gbp', 'customer': session.customer,
            'payment_method': deposit.payment_method,
            'metadata': {'booking_id': booking_id, 'purpose': 'hire_balance'},
        }, options={'idempotency_key': 'balance-create-' + booking_id})
        with app.db() as con:
            stored = con.execute("UPDATE balance_payments SET intent_id=?,status='confirming' WHERE booking_id=? AND status='creating'",
                                 (intent.id, booking_id))
            if stored.rowcount != 1:
                return
    else:
        intent = client.v1.payment_intents.retrieve(row['intent_id'])
    if intent.status == 'requires_confirmation':
        with app.db() as con:
            current = con.execute('SELECT status,intent_id,created_at FROM balance_payments WHERE booking_id=?',
                                  (booking_id,)).fetchone()
            if not current or current['intent_id'] != intent.id or current['status'] not in ('confirming', 'processing'):
                return
            if datetime.now(timezone.utc) - datetime.fromisoformat(current['created_at']) > timedelta(hours=20):
                con.execute("""UPDATE balance_payments SET status='needs_review',last_error='Confirmation outcome unresolved'
                    WHERE booking_id=? AND intent_id=? AND status IN ('confirming','processing')""", (booking_id, intent.id))
                return
        try:
            intent = client.v1.payment_intents.confirm(intent.id, {'off_session': True},
                options={'idempotency_key': 'balance-confirm-' + booking_id})
        except stripe.CardError:
            intent = client.v1.payment_intents.retrieve(intent.id)
    if intent.status == 'succeeded':
        mark_paid(booking_id, intent.id, intent.amount_received)
    elif intent.status in ('requires_payment_method', 'requires_action', 'canceled'):
        if intent.status != 'canceled':
            intent = client.v1.payment_intents.cancel(intent.id)
        if intent.status != 'canceled':
            raise ValueError('Balance intent could not be canceled safely')
        with app.db() as con:
            changed = con.execute("""UPDATE balance_payments SET status='needs_manual',last_error='Automatic payment failed'
                WHERE booking_id=? AND intent_id=? AND status IN ('confirming','processing')""", (booking_id, intent.id))
            if changed.rowcount == 1:
                con.execute("INSERT OR IGNORE INTO email_outbox(booking_id,kind) VALUES (?, 'balance_due')", (booking_id,))
    else:
        with app.db() as con:
            con.execute("""UPDATE balance_payments SET status='processing',last_error=?
                WHERE booking_id=? AND intent_id=? AND status IN ('confirming','processing')""",
                        (intent.status, booking_id, intent.id))


def missing_payment_method(booking_id, previous_status, reason):
    status = 'needs_manual' if previous_status == 'scheduled' else 'needs_review'
    with app.db() as con:
        con.execute("UPDATE balance_payments SET status=?,last_error=? WHERE booking_id=? AND status='creating'",
                    (status, reason, booking_id))


def record_heartbeat():
    with app.db() as con:
        con.execute('''INSERT INTO worker_heartbeats(name,completed_at) VALUES ('balance',?)
            ON CONFLICT(name) DO UPDATE SET completed_at=excluded.completed_at''',
            (datetime.now(timezone.utc).isoformat(),))


def run_once(now=None):
    if os.getenv('BALANCE_COLLECTION_ENABLED') != '1':
        return
    if app.DATABASE_URL:
        with psycopg.connect(app.DATABASE_URL, autocommit=True, connect_timeout=10) as lock:
            if not lock.execute('SELECT pg_try_advisory_lock(%s)', (POSTGRES_WORKER_LOCK,)).fetchone()[0]:
                log.warning('Another balance worker is running; skipping this cycle')
                return
            collect_due(now)
            record_heartbeat()
        return
    lock_path = app.DB_PATH.with_name(app.DB_PATH.name + '.balance-lock')
    with closing(sqlite3.connect(lock_path, timeout=0)) as lock:
        try:
            lock.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:
            if str(exc) in ('database is locked', 'database table is locked'):
                log.warning('Another balance worker is running; skipping this cycle')
                return
            raise
        collect_due(now)
        record_heartbeat()


def collect_due(now=None):
    now = now or datetime.now(timezone.utc)
    with app.db() as con:
        con.execute('''INSERT OR IGNORE INTO balance_payments(booking_id,status,created_at)
            SELECT id,'needs_manual',? FROM bookings
            WHERE payment_option='deposit' AND status='deposit_paid_pending_confirmation'
              AND paid_pence < subtotal_pence''', (now.isoformat(),))
        rows = con.execute('''SELECT b.id,b.start_date,b.terms_version,p.status FROM bookings b
            JOIN balance_payments p ON p.booking_id=b.id
            WHERE b.status='deposit_paid_pending_confirmation' AND b.paid_pence < b.subtotal_pence''').fetchall()
    client = stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])
    for row in rows:
        if not due(row['start_date'], now, midnight=row['terms_version'] in MIDNIGHT_CONSENT_TERMS_VERSIONS):
            continue
        try:
            process_booking(row['id'], client)
            with app.db() as con:
                status = con.execute('SELECT status FROM balance_payments WHERE booking_id=?', (row['id'],)).fetchone()['status']
                if status == 'needs_manual':
                    con.execute("INSERT OR IGNORE INTO email_outbox(booking_id,kind) VALUES (?, 'balance_due')", (row['id'],))
                    email = con.execute('''SELECT status,claimed_at FROM email_outbox
                        WHERE booking_id=? AND kind='balance_due' ''', (row['id'],)).fetchone()
                else:
                    email = None
            email_due = email and (email['status'] == 'pending' or
                (email['status'] in ('failed', 'sending') and
                 (not email['claimed_at'] or datetime.now(timezone.utc) - datetime.fromisoformat(email['claimed_at']) >=
                  timedelta(minutes=15 if email['status'] == 'failed' else 5))))
            if email_due:
                with app.app.test_request_context('/'):
                    app.send_booking_email(row['id'], 'balance_due')
        except (stripe.StripeError, ValueError, KeyError) as exc:
            log.exception('Balance reconciliation failed for booking %s', row['id'])
            with app.db() as con:
                con.execute('UPDATE balance_payments SET last_error=? WHERE booking_id=?', (type(exc).__name__, row['id']))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            run_once()
        except (stripe.StripeError, psycopg.Error, sqlite3.Error, OSError, KeyError) as exc:
            log.exception('Balance worker failed: %s', type(exc).__name__)
        time.sleep(60)
