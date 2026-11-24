"""Run against the dedicated local PostgreSQL test database only."""
import json
import io
import os
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from PIL import Image

database_url = os.environ.get('JUA_TEST_DATABASE_URL', '')
url = urlsplit(database_url)
if (url.scheme not in ('postgres', 'postgresql') or url.hostname != '127.0.0.1'
        or url.port != 55432 or url.username != 'jua_test' or url.path != '/jua_test'
        or url.query or url.fragment):
    raise SystemExit('JUA_TEST_DATABASE_URL must point to the dedicated local jua_test database on port 55432.')

os.environ.update(
    DATABASE_URL=database_url,
    AZURE_STORAGE_ACCOUNT_URL='',
    WEBSITE_INSTANCE_ID='',
    SECRET_KEY='isolated-postgres-test',
    STRIPE_SECRET_KEY='sk_test_mock',
    STRIPE_WEBHOOK_SECRET='whsec_mock',
    BALANCE_COLLECTION_ENABLED='1',
    EMAILJS_SERVICE_ID='',
    EMAILJS_TEMPLATE_ID='',
    EMAILJS_PUBLIC_KEY='',
    EMAILJS_PRIVATE_KEY='',
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import balance_worker
import deploy.clear_test_requests as cleanup


class PostgresBalanceChecks(unittest.TestCase):
    def setUp(self):
        self.booking_id = 'pg-test-' + uuid.uuid4().hex
        self.now = datetime(2030, 7, 4, 23, 0, tzinfo=timezone.utc)
        with app.db() as con:
            con.execute('''INSERT INTO bookings
                (id,created_at,name,email,phone,start_date,end_date,items_json,subtotal_pence,
                 payment_option,due_pence,paid_pence,status,stripe_session_id,terms_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (self.booking_id, self.now.isoformat(), 'Test', 'test@example.com', '',
                 '2030-07-05', '2030-07-05', json.dumps([]), 1000, 'deposit', 300, 300,
                 'deposit_paid_pending_confirmation', 'cs_test_mock', app.TERMS_VERSION))
            con.execute('''INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES (?,?,?,?)''', (self.booking_id, self.now.isoformat(), 'scheduled', self.now.isoformat()))

    def tearDown(self):
        with app.db() as con:
            con.execute('DELETE FROM email_outbox WHERE booking_id=?', (self.booking_id,))
            con.execute('DELETE FROM balance_payments WHERE booking_id=?', (self.booking_id,))
            con.execute('DELETE FROM bookings WHERE id=?', (self.booking_id,))

    def client(self, factory):
        client = factory.return_value
        client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(
            customer='cus_test', payment_intent='pi_deposit')
        client.v1.payment_intents.retrieve.return_value = SimpleNamespace(
            status='succeeded', payment_method='pm_test',
            latest_charge={'amount_refunded': 0, 'disputed': False})
        client.v1.payment_intents.create.return_value = SimpleNamespace(
            id='pi_balance', status='requires_confirmation')
        return client

    def test_midnight_charge_records_once(self):
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = self.client(factory)
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(
                id='pi_balance', status='succeeded', amount_received=700)
            balance_worker.run_once(self.now)
            balance_worker.run_once(self.now)
            self.assertEqual(client.v1.payment_intents.create.call_count, 1)
            self.assertEqual(client.v1.payment_intents.confirm.call_count, 1)
        with app.db() as con:
            row = con.execute('''SELECT b.paid_pence,p.status FROM bookings b
                JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (self.booking_id,)).fetchone()
            self.assertEqual((row['paid_pence'], row['status']), (1000, 'paid'))

    def test_failed_payment_is_cancelled_before_manual_recovery(self):
        with patch.object(balance_worker.stripe, 'StripeClient') as factory, \
                patch.object(app, 'send_booking_email'):
            client = self.client(factory)
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(id='pi_balance', status='requires_action')
            client.v1.payment_intents.cancel.return_value = SimpleNamespace(id='pi_balance', status='canceled')
            balance_worker.run_once(self.now)
            client.v1.payment_intents.cancel.assert_called_once_with('pi_balance')
        with app.db() as con:
            row = con.execute('SELECT status FROM balance_payments WHERE booking_id=?', (self.booking_id,)).fetchone()
            email = con.execute("SELECT kind FROM email_outbox WHERE booking_id=?", (self.booking_id,)).fetchone()
            self.assertEqual(row['status'], 'needs_manual')
            self.assertEqual(email['kind'], 'balance_due')

    def test_concurrent_workers_cannot_both_process(self):
        entered, release = threading.Event(), threading.Event()
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = self.client(factory)
            def create(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Timed out waiting for the competing worker')
                return SimpleNamespace(id='pi_balance', status='requires_confirmation')
            client.v1.payment_intents.create.side_effect = create
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(
                id='pi_balance', status='succeeded', amount_received=700)
            with ThreadPoolExecutor(max_workers=1) as executor:
                running = executor.submit(balance_worker.run_once, self.now)
                try:
                    self.assertTrue(entered.wait(10), 'First worker did not reach Stripe')
                    balance_worker.run_once(self.now)
                finally:
                    release.set()
                running.result(timeout=10)
            self.assertEqual(client.v1.payment_intents.create.call_count, 1)
            self.assertEqual(client.v1.payment_intents.confirm.call_count, 1)

    def test_admin_stop_and_worker_claim_are_mutually_exclusive(self):
        entered, release = threading.Event(), threading.Event()
        app.app.config.update(TESTING=True, SECRET_KEY='isolated-postgres-test')
        web = app.app.test_client()
        with web.session_transaction() as session:
            session['admin'] = True
            session['csrf_token'] = 'test-token'
        with patch.dict(os.environ, {'ADMIN_USERNAME': 'test', 'ADMIN_PASSWORD': 'test'}), \
                patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = self.client(factory)
            def retrieve(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Timed out waiting for admin stop')
                return SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
            client.v1.checkout.sessions.retrieve.side_effect = retrieve
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(
                id='pi_balance', status='succeeded', amount_received=700)
            with ThreadPoolExecutor(max_workers=1) as executor:
                running = executor.submit(balance_worker.process_booking, self.booking_id, client)
                try:
                    self.assertTrue(entered.wait(10), 'Worker did not claim the balance')
                    response = web.post(f'/admin/bookings/{self.booking_id}/balance/stop',
                                        data={'csrf_token': 'test-token'})
                    self.assertEqual(response.status_code, 303)
                    with app.db() as con:
                        self.assertEqual(con.execute('SELECT status FROM balance_payments WHERE booking_id=?',
                                                     (self.booking_id,)).fetchone()['status'], 'creating')
                finally:
                    release.set()
                running.result(timeout=10)
            with app.db() as con:
                self.assertEqual(con.execute('SELECT status FROM balance_payments WHERE booking_id=?',
                                             (self.booking_id,)).fetchone()['status'], 'paid')


class PostgresCleanupChecks(unittest.TestCase):
    def test_cleanup_is_atomic_and_preserves_newer_requests(self):
        schema = 'cleanup_test_' + uuid.uuid4().hex
        connect = app.psycopg.connect
        def drop_schema():
            with connect(database_url, autocommit=True) as connection:
                connection.execute(app.psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(
                    app.psycopg.sql.Identifier(schema)))
        with connect(database_url, autocommit=True) as setup:
            setup.execute(app.psycopg.sql.SQL('CREATE SCHEMA {}').format(app.psycopg.sql.Identifier(schema)))
            self.addCleanup(drop_schema)
            setup.execute(app.psycopg.sql.SQL('SET search_path TO {}').format(app.psycopg.sql.Identifier(schema)))
            setup.execute('''CREATE TABLE bookings (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                payment_option TEXT NOT NULL, status TEXT NOT NULL, paid_pence INTEGER NOT NULL,
                stripe_session_id TEXT)''')
            for table in ('balance_payments', 'return_events', 'email_outbox', 'booking_items'):
                extra = ', status TEXT' if table == 'email_outbox' else ''
                setup.execute(f'CREATE TABLE {table} (booking_id TEXT REFERENCES bookings(id){extra})')
            setup.execute('CREATE TABLE blocker (booking_id TEXT REFERENCES bookings(id))')
            setup.execute("""INSERT INTO bookings VALUES
                ('a', '2026-01-01T00:00:00+00:00', 'full', 'payment_failed', 0, 'cs_a'),
                ('b', '2026-01-01T00:00:00+00:00', 'full', 'payment_failed', 0, 'cs_b'),
                ('c', '2026-01-01T00:00:00+00:00', 'quote', 'quote_requested', 0, NULL),
                ('new', '2027-01-01T00:00:00+00:00', 'quote', 'quote_requested', 0, NULL)""")
            for table in ('return_events', 'email_outbox', 'booking_items'):
                setup.execute(f"INSERT INTO {table} (booking_id) VALUES ('a'), ('c'), ('new')")
            setup.execute("INSERT INTO blocker VALUES ('a')")
            def isolated_connect(*args, **kwargs):
                return connect(*args, options='-csearch_path=' + schema, **kwargs)
            def checkout(session_id):
                return SimpleNamespace(status='expired', payment_status='unpaid',
                                       client_reference_id=session_id[-1])
            with patch.object(cleanup.psycopg, 'connect', side_effect=isolated_connect), \
                    patch.object(cleanup, 'HOST', '127.0.0.1'), patch.object(cleanup, 'DATABASE', 'jua_test'), \
                    patch.dict(os.environ, {'JUA_CLEANUP_DB_URL': database_url, 'JUA_CLEANUP_STRIPE_KEY': 'mock'}), \
                    patch('builtins.input', return_value='DELETE 3 TEST REQUESTS'), \
                    patch.object(cleanup.stripe, 'StripeClient') as client:
                client.return_value.v1.checkout.sessions.retrieve.side_effect = checkout
                with self.assertRaises(app.psycopg.errors.ForeignKeyViolation):
                    cleanup.main()
                self.assertEqual(setup.execute('SELECT COUNT(*) FROM bookings').fetchone()[0], 4)
                self.assertEqual(setup.execute('SELECT COUNT(*) FROM booking_items').fetchone()[0], 3)
                setup.execute('DELETE FROM blocker')
                cleanup.main()
            self.assertEqual(setup.execute('SELECT id FROM bookings').fetchall(), [('new',)])
            for table in ('return_events', 'email_outbox', 'booking_items'):
                self.assertEqual(setup.execute(f'SELECT booking_id FROM {table}').fetchall(), [('new',)])


class PostgresImageChecks(unittest.TestCase):
    def setUp(self):
        identifier = uuid.uuid4().hex
        self.category_id = 'pg-image-category-' + identifier
        self.product_id = 'pg-image-product-' + identifier
        self.old_filename = uuid.uuid4().hex + '.png'
        self.temp = tempfile.TemporaryDirectory()
        self.upload_path = Path(self.temp.name)
        self.upload_patch = patch.object(app, 'UPLOAD_PATH', self.upload_path)
        self.upload_patch.start()
        self.addCleanup(self.upload_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.env_patch = patch.dict(os.environ, {'ADMIN_USERNAME': 'test', 'ADMIN_PASSWORD': 'test'})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.upload_path.joinpath(self.old_filename).write_bytes(b'old image')
        with app.db() as con:
            con.execute('INSERT INTO categories(id,name) VALUES (?,?)', (self.category_id, self.category_id))
            con.execute('''INSERT INTO products(id,category_id,name,description,price_pence,stock,image_url)
                VALUES (?,?,?,?,?,?,?)''', (self.product_id, self.category_id, 'Test', 'Test', 100, 1,
                                             '/uploads/' + self.old_filename))
        app.app.config.update(TESTING=True, SECRET_KEY='isolated-postgres-test')
        self.web = app.app.test_client()
        with self.web.session_transaction() as session:
            session['admin'] = True
            session['csrf_token'] = 'test-token'

    def tearDown(self):
        with app.db() as con:
            row = con.execute('SELECT image_url FROM products WHERE id=?', (self.product_id,)).fetchone()
            con.execute('DELETE FROM products WHERE id=?', (self.product_id,))
            con.execute('DELETE FROM categories WHERE id=?', (self.category_id,))
            con.execute('DELETE FROM image_deletions WHERE image_url=?', ('/uploads/' + self.old_filename,))
            if row:
                con.execute('DELETE FROM image_deletions WHERE image_url=?', (row['image_url'],))

    def test_replacement_and_removal_delete_only_unreferenced_images(self):
        image = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(image, 'PNG')
        image.seek(0)
        response = self.web.post(f'/admin/products/{self.product_id}/edit', data={
            'csrf_token': 'test-token', 'name': 'Test', 'description': 'Test',
            'category_id': self.category_id, 'price': '1.00', 'stock': '1',
            'image': (image, 'image.png'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.upload_path.joinpath(self.old_filename).exists())
        with app.db() as con:
            image_url = con.execute('SELECT image_url FROM products WHERE id=?', (self.product_id,)).fetchone()['image_url']
        new_filename = image_url.rsplit('/', 1)[1]
        self.assertTrue(self.upload_path.joinpath(new_filename).exists())
        self.assertEqual(self.web.post(f'/admin/products/{self.product_id}/delete',
                                       data={'csrf_token': 'test-token'}).status_code, 302)
        self.assertFalse(self.upload_path.joinpath(new_filename).exists())
        with app.db() as con:
            self.assertFalse(con.execute('SELECT 1 FROM image_deletions WHERE image_url=?', (image_url,)).fetchone())

    def test_catalog_availability_on_postgresql(self):
        response = self.web.get('/availability/catalog')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['available'][self.product_id], 1)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')


if __name__ == '__main__':
    unittest.main(verbosity=2)
