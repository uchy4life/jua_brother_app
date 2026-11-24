"""One-time, user-confirmed cleanup of the three pre-launch test requests."""
import os
from collections import Counter

import psycopg
from psycopg.rows import dict_row
import stripe

CUTOFF = '2026-11-24T14:55:40.993+00:00'
HOST = 'jua-rental-pg-north-eu-2026.postgres.database.azure.com'
DATABASE = 'juarental'
EXPECTED = Counter({('full', 'payment_failed'): 2, ('quote', 'quote_requested'): 1})
BOOKINGS_SQL = '''SELECT id, created_at, payment_option, status, paid_pence, stripe_session_id
    FROM bookings WHERE created_at::timestamptz < %s::timestamptz ORDER BY id'''


def validate_bookings(rows):
    if Counter((r['payment_option'], r['status']) for r in rows) != EXPECTED:
        raise RuntimeError('The older request counts or statuses changed; no records were deleted.')
    for row in rows:
        if row['paid_pence'] != 0:
            raise RuntimeError('A request has a recorded payment; no records were deleted.')
        if bool(row['stripe_session_id']) != (row['payment_option'] == 'full'):
            raise RuntimeError('Unexpected Checkout reference; no records were deleted.')


def validate_session(booking, checkout):
    if (checkout.status != 'expired' or checkout.payment_status != 'unpaid'
            or checkout.client_reference_id != booking['id']):
        raise RuntimeError('A Checkout session is not expired, unpaid and matched; no records were deleted.')


def main():
    with psycopg.connect(os.environ['JUA_CLEANUP_DB_URL'], autocommit=True,
                         row_factory=dict_row, connect_timeout=10) as con:
        if con.info.host != HOST or con.info.dbname != DATABASE:
            raise RuntimeError('Unexpected database; no records were deleted.')
        rows = con.execute(BOOKINGS_SQL, (CUTOFF,)).fetchall()
        validate_bookings(rows)
        client = stripe.StripeClient(os.environ['JUA_CLEANUP_STRIPE_KEY'])
        for row in rows:
            if row['stripe_session_id']:
                checkout = client.v1.checkout.sessions.retrieve(row['stripe_session_id'])
                validate_session(row, checkout)
            print(f"{row['id'][:12]} | {row['payment_option']} | {row['status']}")
        print('Verify that all three references above are your test requests in admin.')
        if input('Type DELETE 3 TEST REQUESTS to proceed: ') != 'DELETE 3 TEST REQUESTS':
            print('Cancelled; no records were deleted.')
            return
        ids = [row['id'] for row in rows]
        with con.transaction():
            for lock in (581234902, 581234901):
                if not con.execute('SELECT pg_try_advisory_xact_lock(%s) AS locked', (lock,)).fetchone()['locked']:
                    raise RuntimeError('The worker or inventory is busy; no records were deleted. Try again later.')
            current = con.execute(BOOKINGS_SQL + ' FOR UPDATE', (CUTOFF,)).fetchall()
            if current != rows:
                raise RuntimeError('The older requests changed; no records were deleted.')
            validate_bookings(current)
            if con.execute('SELECT 1 FROM balance_payments WHERE booking_id = ANY(%s) LIMIT 1', (ids,)).fetchone():
                raise RuntimeError('A balance record exists; no records were deleted.')
            emails = con.execute('SELECT status FROM email_outbox WHERE booking_id = ANY(%s) FOR UPDATE', (ids,)).fetchall()
            if any(email['status'] == 'sending' for email in emails):
                raise RuntimeError('An email is being sent; no records were deleted.')
            for table in ('return_events', 'email_outbox', 'booking_items'):
                con.execute(f'DELETE FROM {table} WHERE booking_id = ANY(%s)', (ids,))
            deleted = con.execute('DELETE FROM bookings WHERE id = ANY(%s)', (ids,)).rowcount
            if deleted != 3:
                raise RuntimeError('Unexpected delete count; transaction rolled back.')
        print('Deleted 3 test requests and their related records. Stripe records were not deleted.')


if __name__ == '__main__':
    main()
