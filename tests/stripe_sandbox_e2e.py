"""Exercise a real Stripe test-mode deposit and balance against an isolated PostgreSQL database."""
import getpass
import os
import sys
import threading
import time
import uuid
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

database_url = os.environ.get('JUA_TEST_DATABASE_URL', '')
url = urlsplit(database_url)
if (url.scheme not in ('postgres', 'postgresql') or url.hostname != '127.0.0.1'
        or url.port != 55432 or url.username != 'jua_test' or url.path != '/jua_test'
        or url.query or url.fragment):
    raise SystemExit('JUA_TEST_DATABASE_URL must point to the dedicated local jua_test database on port 55432.')

key = getpass.getpass('Stripe sandbox/test-mode secret key (input hidden): ').strip()
if not key.startswith(('sk_test_', 'rk_test_')):
    raise SystemExit('A Stripe test-mode secret or restricted key is required; live keys are refused.')
webhook_secret = getpass.getpass('Stripe CLI listener signing secret (input hidden): ').strip()
if not webhook_secret.startswith('whsec_'):
    raise SystemExit('A Stripe CLI webhook signing secret is required.')

os.environ.update(
    DATABASE_URL=database_url,
    AZURE_STORAGE_ACCOUNT_URL='',
    WEBSITE_INSTANCE_ID='',
    SECRET_KEY=uuid.uuid4().hex,
    STRIPE_SECRET_KEY=key,
    STRIPE_WEBHOOK_SECRET=webhook_secret,
    BALANCE_COLLECTION_ENABLED='1',
    PUBLIC_BASE_URL='http://127.0.0.1:18766',
    REDIRECT_FROM_HOSTS='',
    EMAILJS_SERVICE_ID='',
    EMAILJS_TEMPLATE_ID='',
    EMAILJS_PUBLIC_KEY='',
    EMAILJS_PRIVATE_KEY='',
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import balance_worker
import stripe
from werkzeug.serving import make_server


def main():
    identifier = uuid.uuid4().hex
    category_id = 'sandbox-category-' + identifier
    product_id = 'sandbox-product-' + identifier
    email = f'test+{identifier}@example.com'
    booking_id = None
    server = make_server('127.0.0.1', 18766, app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with app.db() as con:
            con.execute('INSERT INTO categories(id,name) VALUES (?,?)', (category_id, category_id))
            con.execute('''INSERT INTO products(id,category_id,name,description,price_pence,stock,image_url)
                VALUES (?,?,?,?,?,?,?)''', (product_id, category_id, 'Sandbox test item', 'Test only', 1000, 1, ''))
        balance_worker.record_heartbeat()
        start = app.business_today() + timedelta(days=7)
        with patch.object(app, 'send_booking_email'):
            response = app.app.test_client().post('/book', data={
                'customer_name': 'Sandbox Test', 'email': email, 'phone': '',
                'start_date': start.isoformat(), 'end_date': start.isoformat(),
                'payment_option': 'deposit', 'balance_consent': 'yes',
                'terms_accepted': app.TERMS_VERSION, 'qty_' + product_id: '1',
                'price_' + product_id: '1000',
            })
            if response.status_code != 303:
                raise RuntimeError(f'Test deposit checkout could not be created (HTTP {response.status_code}).')
            with app.db() as con:
                booking = con.execute('SELECT id,stripe_session_id FROM bookings WHERE email=?', (email,)).fetchone()
            if not booking or not booking['stripe_session_id']:
                raise RuntimeError('Test booking or Checkout Session was not saved.')
            booking_id = booking['id']
            session_id = booking['stripe_session_id']
            stripe_client = stripe.StripeClient(key)
            session = stripe_client.v1.checkout.sessions.retrieve(session_id)
            if session.livemode or session.amount_total != 300 or session.currency != 'gbp':
                raise RuntimeError('Stripe session is not the expected test-mode £3 deposit.')
            print('Open this private Stripe TEST checkout link in your browser (do not share it):')
            print(response.location, flush=True)
            print('Complete it with a Stripe test card; waiting up to five minutes for the signed webhook...', flush=True)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                with app.db() as con:
                    booking = con.execute('SELECT status,paid_pence FROM bookings WHERE id=?', (booking_id,)).fetchone()
                if booking and booking['status'] == 'deposit_paid_pending_confirmation':
                    break
                time.sleep(2)
            else:
                raise RuntimeError('The test deposit webhook was not received. Check the Stripe CLI listener.')
            if booking['paid_pence'] != 300:
                raise RuntimeError('Recorded deposit amount was incorrect.')
            session = stripe_client.v1.checkout.sessions.retrieve(session_id)
            if session.livemode or session.payment_status != 'paid' or not session.customer:
                raise RuntimeError('Stripe did not confirm the paid test deposit and customer.')
            due = datetime.combine(start, datetime_time.min, tzinfo=ZoneInfo('Europe/London'))
            if not balance_worker.due(start.isoformat(), due, midnight=True):
                raise RuntimeError('The midnight balance schedule did not match the test booking.')
            balance_worker.process_booking(booking_id, stripe_client)
            balance_worker.process_booking(booking_id, stripe_client)
            with app.db() as con:
                result = con.execute('''SELECT b.paid_pence,p.status,p.intent_id FROM bookings b
                    JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
            if not result or result['paid_pence'] != 1000 or result['status'] != 'paid' or not result['intent_id']:
                raise RuntimeError('The test balance was not recorded as paid; inspect the test booking before retrying.')
            intent = stripe_client.v1.payment_intents.retrieve(result['intent_id'])
            if (intent.livemode or intent.status != 'succeeded' or intent.amount_received != 700
                    or intent.currency != 'gbp' or intent.metadata.get('booking_id') != booking_id):
                raise RuntimeError('Stripe did not confirm the expected test-mode £7 balance payment.')
            customer = session.customer
            intents = stripe_client.v1.payment_intents.list({'customer': customer, 'limit': 100})
            balances = [item for item in intents.data if item.metadata.get('booking_id') == booking_id
                        and item.metadata.get('purpose') == 'hire_balance']
            if len(balances) != 1 or balances[0].id != result['intent_id']:
                raise RuntimeError('The sandbox contains an unexpected number of balance payment intents.')
            print('PASS: test deposit £3, signed webhook, midnight due-time rule, one £7 off-session balance charge, and repeat-run check.', flush=True)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        with app.db() as con:
            if not booking_id:
                row = con.execute('SELECT id FROM bookings WHERE email=?', (email,)).fetchone()
                booking_id = row['id'] if row else None
            if booking_id:
                con.execute('DELETE FROM email_outbox WHERE booking_id=?', (booking_id,))
                con.execute('DELETE FROM balance_payments WHERE booking_id=?', (booking_id,))
                con.execute('DELETE FROM booking_items WHERE booking_id=?', (booking_id,))
                con.execute('DELETE FROM bookings WHERE id=?', (booking_id,))
            con.execute('DELETE FROM products WHERE id=?', (product_id,))
            con.execute('DELETE FROM categories WHERE id=?', (category_id,))


if __name__ == '__main__':
    main()
