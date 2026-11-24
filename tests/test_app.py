import io
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from PIL import Image

_initial = tempfile.TemporaryDirectory()
os.environ.update(
    DATABASE_PATH=str(Path(_initial.name) / 'initial.db'),
    SECRET_KEY='test-secret',
    ADMIN_USERNAME='admin',
    ADMIN_PASSWORD='correct-password',
    STRIPE_SECRET_KEY='sk_test_example',
    STRIPE_WEBHOOK_SECRET='whsec_example',
    PUBLIC_BASE_URL='https://rental.example',
    EMAILJS_SERVICE_ID='service_test',
    EMAILJS_TEMPLATE_ID='template_test',
    EMAILJS_PUBLIC_KEY='public_test',
    EMAILJS_PRIVATE_KEY='private_test',
    BALANCE_COLLECTION_ENABLED='1',
)
import app
import balance_worker


class RentalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / 'bookings.db'
        app.UPLOAD_PATH = Path(self.temp.name) / 'uploads'
        app.app.config.update(TESTING=True, SECRET_KEY='test-secret')
        app.init_db()
        balance_worker.record_heartbeat()
        self.client = app.app.test_client()
        emailjs_patch = patch.object(app, 'urlopen')
        self.emailjs = emailjs_patch.start()
        self.emailjs.return_value.__enter__.return_value.status = 200
        self.addCleanup(emailjs_patch.stop)
        self.rate_limit_sleep = patch.object(app.time, 'sleep')
        self.rate_limit_sleep.start()
        self.addCleanup(self.rate_limit_sleep.stop)

    def tearDown(self):
        self.temp.cleanup()

    def csrf(self, response):
        return re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()

    def login(self):
        token = self.csrf(self.client.get('/admin/login'))
        response = self.client.post('/admin/login', data={
            'csrf_token': token, 'username': 'admin', 'password': 'correct-password',
        })
        self.assertEqual(response.status_code, 302)
        return self.csrf(self.client.get('/admin'))

    def booking(self, **overrides):
        start = date.today() + timedelta(days=1)
        data = {
            'customer_name': 'Test User', 'email': 'test@example.com',
            'start_date': start.isoformat(), 'end_date': (start + timedelta(days=2)).isoformat(),
            'payment_option': 'full', 'qty_chiavari': '2', 'price_chiavari': '350',
            'balance_consent': 'yes', 'terms_accepted': app.TERMS_VERSION,
        }
        data.update(overrides)
        return self.client.post('/book', data=data)

    def test_seed_only_once_and_preserves_old_booking_ids(self):
        with app.db() as con:
            con.execute("UPDATE products SET name='Updated chair' WHERE id='chiavari'")
        app.init_db()
        response = self.client.get('/')
        self.assertIn(b'Updated chair', response.data)
        self.assertNotIn(b'Chiavari Chairs', response.data)
        self.assertEqual(len(app.catalog()), 6)

    def test_storefront_product_prices_show_pound_symbol(self):
        response = self.client.get('/')
        self.assertIn('£3.50 per item per day', response.get_data(as_text=True))
        self.assertNotIn('?3.50 per item per day', response.get_data(as_text=True))

    def test_favicon_is_linked_and_served(self):
        for path in ('/', '/admin/login'):
            self.assertIn(b'href="/static/favicon.svg?v=coffee-circle" type="image/svg+xml"', self.client.get(path).data)
        icon = self.client.get('/static/favicon.svg?v=coffee-circle')
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.mimetype, 'image/svg+xml')
        self.assertIn(b'<circle cx="32" cy="32" r="32" fill="#6f4e37"/>', icon.data)
        icon.close()

    def test_terms_are_required_and_recorded_for_quotes_and_checkouts(self):
        for option in ('quote', 'full'):
            with self.subTest(option=option):
                self.assertEqual(self.booking(payment_option=option, terms_accepted='').status_code, 400)
                self.assertEqual(self.booking(payment_option=option, terms_accepted='old-version').status_code, 400)
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_terms', url='https://checkout.stripe.test')
            self.assertEqual(self.booking().status_code, 303)
        self.assertEqual(self.booking(payment_option='quote').status_code, 302)
        with app.db() as con:
            rows = con.execute('SELECT terms_version,terms_accepted_at FROM bookings').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['terms_version'] == app.TERMS_VERSION and row['terms_accepted_at'] for row in rows))

    def test_health_check_uses_database(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'status': 'ok'})

    def test_balance_worker_health_requires_recent_heartbeat(self):
        from datetime import datetime, timezone
        with patch.dict(os.environ, {'BALANCE_COLLECTION_ENABLED': '1'}):
            with app.db() as con:
                con.execute("DELETE FROM worker_heartbeats WHERE name='balance'")
            self.assertEqual(self.client.get('/healthz/balance-worker').status_code, 503)
            self.assertEqual(self.booking(payment_option='deposit').status_code, 400)
            balance_worker.record_heartbeat()
            self.assertEqual(self.client.get('/healthz/balance-worker').status_code, 200)
            with app.db() as con:
                con.execute("UPDATE worker_heartbeats SET completed_at=? WHERE name='balance'",
                            ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),))
            self.assertEqual(self.client.get('/healthz/balance-worker').status_code, 503)
            self.assertEqual(self.booking(payment_option='deposit').status_code, 400)
        with patch.dict(os.environ, {'BALANCE_COLLECTION_ENABLED': '0'}):
            self.assertEqual(self.client.get('/healthz/balance-worker').status_code, 503)

    def test_alternate_domain_redirect_preserves_path_and_query(self):
        settings = {'REDIRECT_FROM_HOSTS': 'example.com, www.example.com',
                    'PUBLIC_BASE_URL': 'https://rental.example'}
        with patch.dict(os.environ, settings):
            response = self.client.post('/admin/login?next=%2Fbookings', base_url='https://www.example.com')
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.location, 'https://rental.example/admin/login?next=%2Fbookings')
            self.assertEqual(self.client.get('/healthz', base_url='https://rental.example').status_code, 200)
            self.assertEqual(self.client.get('/healthz', base_url='https://notexample.com').status_code, 200)

    def test_alternate_domain_redirect_rejects_invalid_or_looping_target(self):
        for origin in ('http://rental.example', 'https://example.com', 'https://rental.example/path',
                       'https://[broken', 'https://rental.example:notaport'):
            with self.subTest(origin=origin), patch.dict(os.environ, {
                'REDIRECT_FROM_HOSTS': 'example.com', 'PUBLIC_BASE_URL': origin,
            }):
                self.assertEqual(self.client.get('/', base_url='https://example.com').status_code, 503)

    def test_cart_token_follows_quote_and_success_but_not_cancel(self):
        token = 'a' * 32
        quote = self.booking(payment_option='quote', cart_token=token)
        self.assertIn('checkout=quote', quote.location)
        self.assertIn('cart_token=' + token, quote.location)
        self.assertIn(token.encode(), self.client.get(quote.location).data)
        with patch.object(app.stripe, 'StripeClient') as client:
            create = client.return_value.v1.checkout.sessions.create
            create.return_value = SimpleNamespace(id='cs_cart', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(cart_token=token).status_code, 303)
            params = create.call_args.args[0]
            self.assertIn('checkout=returned&cart_token=' + token, params['success_url'])
            self.assertNotIn('cart_token', params['cancel_url'])
        with app.db() as con:
            booking_id = con.execute("SELECT id FROM bookings WHERE stripe_session_id='cs_cart'").fetchone()[0]
        self.assertIn(token.encode(), self.client.get(f'/booking/{booking_id}?checkout=returned&cart_token={token}').data)
        self.assertNotIn(token.encode(), self.client.get(f'/booking/{booking_id}?checkout=cancelled&cart_token={token}').data)
        with app.db() as con:
            con.execute("UPDATE bookings SET status='payment_failed' WHERE id=?", (booking_id,))
        self.assertNotIn(token.encode(), self.client.get(f'/booking/{booking_id}?checkout=returned&cart_token={token}').data)
        self.assertEqual(self.booking(cart_token='invalid').status_code, 400)

    def test_admin_rechecks_initial_payment_from_stripe_once(self):
        with patch.object(app.stripe, 'StripeClient') as factory:
            factory.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_check', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(payment_option='deposit').status_code, 303)
        with app.db() as con:
            booking = con.execute("SELECT * FROM bookings WHERE stripe_session_id='cs_check'").fetchone()
        csrf = self.login()
        session_data = {'id':'cs_check', 'client_reference_id':booking['id'], 'amount_total':booking['due_pence'],
                        'currency':'gbp', 'payment_status':'unpaid'}
        with patch.object(app.stripe, 'StripeClient') as factory, patch.object(app, 'send_booking_email') as send:
            factory.return_value.v1.checkout.sessions.retrieve.return_value = session_data
            self.client.post(f"/admin/bookings/{booking['id']}/payment/recheck", data={'csrf_token':csrf})
            send.assert_not_called()
            session_data['payment_status'] = 'paid'
            session_data['amount_total'] += 1
            self.client.post(f"/admin/bookings/{booking['id']}/payment/recheck", data={'csrf_token':csrf})
            send.assert_not_called()
            session_data['amount_total'] -= 1
            self.client.post(f"/admin/bookings/{booking['id']}/payment/recheck", data={'csrf_token':csrf})
            self.client.post(f"/admin/bookings/{booking['id']}/payment/recheck", data={'csrf_token':csrf})
            send.assert_called_once_with(booking['id'], 'paid')
        with app.db() as con:
            row = con.execute('SELECT status,paid_pence FROM bookings WHERE id=?', (booking['id'],)).fetchone()
            self.assertEqual((row['status'],row['paid_pence']), ('deposit_paid_pending_confirmation',booking['due_pence']))
            self.assertEqual(con.execute('SELECT status FROM balance_payments WHERE booking_id=?',(booking['id'],)).fetchone()[0], 'scheduled')
        self.assertIn(b'Confirm return', self.client.get('/admin').data)

    def test_existing_database_migrates_and_keeps_reservations(self):
        app.DB_PATH = Path(self.temp.name) / 'legacy.db'
        start = (date.today() + timedelta(days=1)).isoformat()
        with closing(sqlite3.connect(app.DB_PATH)) as con, con:
            con.execute('''CREATE TABLE bookings (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
                email TEXT NOT NULL, phone TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                items_json TEXT NOT NULL, subtotal_pence INTEGER NOT NULL,
                payment_option TEXT NOT NULL, due_pence INTEGER NOT NULL,
                paid_pence INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                stripe_session_id TEXT UNIQUE)''')
            con.execute('''INSERT INTO bookings VALUES
                ('old', '2020-01-01', 'Previous', 'old@example.com', '', ?, ?,
                 ?, 35000, 'full', 35000, 35000, 'paid_pending_confirmation', 'cs_old')''',
                (start, start, json.dumps([{'id': 'chiavari', 'name': 'Chiavari Chairs', 'quantity': 100}])))
        app.init_db()
        self.assertEqual(self.booking().status_code, 400)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT subtotal_pence FROM bookings WHERE id='old'").fetchone()[0], 35000)
        self.assertEqual(len(app.catalog()), 6)

    def test_existing_booking_items_gain_original_booking_dates(self):
        self.insert_paid('legacy_dates', date(2030, 1, 2), date(2030, 1, 4))
        with app.db() as con:
            con.execute('ALTER TABLE booking_items RENAME TO new_booking_items')
            con.execute('''CREATE TABLE booking_items (booking_id TEXT NOT NULL,product_id TEXT NOT NULL,
                name TEXT NOT NULL,quantity INTEGER NOT NULL,returned INTEGER NOT NULL DEFAULT 0,
                return_requested INTEGER NOT NULL DEFAULT 0,PRIMARY KEY (booking_id,product_id))''')
            con.execute('''INSERT INTO booking_items SELECT booking_id,product_id,name,quantity,returned,return_requested
                FROM new_booking_items''')
            con.execute('DROP TABLE new_booking_items')
        app.init_db()
        app.init_db()
        with app.db() as con:
            item = con.execute("SELECT start_date,end_date FROM booking_items WHERE booking_id='legacy_dates'").fetchone()
            self.assertEqual((item['start_date'], item['end_date']), ('2030-01-02', '2030-01-04'))

    def test_admin_auth_csrf_catalog_and_upload(self):
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.assertEqual(self.client.post('/admin/login', data={}).status_code, 400)
        token = self.login()
        self.client.post('/admin/categories', data={'csrf_token': token, 'name': 'Linen'})
        with app.db() as con:
            category_id = con.execute("SELECT id FROM categories WHERE name='Linen'").fetchone()[0]
        image = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(image, 'PNG')
        image.seek(0)
        response = self.client.post('/admin/products/new', data={
            'csrf_token': token, 'name': 'Tablecloth', 'description': 'White linen',
            'category_id': category_id, 'price': '2.75', 'stock': '12', 'image': (image, 'image.png'),
        })
        self.assertEqual(response.status_code, 302)
        with app.db() as con:
            product = con.execute("SELECT * FROM products WHERE name='Tablecloth'").fetchone()
        self.assertEqual(product['price_pence'], 275)
        image_response = self.client.get(product['image_url'])
        self.assertEqual(image_response.status_code, 200)
        image_response.close()
        self.assertIn(b'Tablecloth', self.client.get('/').data)
        response = self.client.post(f"/admin/products/{product['id']}/edit", data={
            'csrf_token': token, 'name': 'Updated linen', 'description': 'New description',
            'category_id': category_id, 'price': '3.50', 'stock': '10',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn(b'Updated linen', self.client.get('/').data)
        self.client.post(f"/admin/products/{product['id']}/delete", data={'csrf_token': token})
        self.assertNotIn(b'Updated linen', self.client.get('/').data)
        self.assertEqual(self.client.post(f'/admin/categories/{category_id}/delete', data={'csrf_token': token}).status_code, 302)
        with app.db() as con:
            self.assertIsNone(con.execute('SELECT id FROM categories WHERE id=?', (category_id,)).fetchone())

    def test_invalid_images_and_duplicate_categories(self):
        token = self.login()
        self.client.post('/admin/categories', data={'csrf_token': token, 'name': 'Chairs'})
        self.assertIn(b'already exists', self.client.get('/admin').data)
        response = self.client.post('/admin/products/new', data={
            'csrf_token': token, 'name': 'Fake', 'description': 'Invalid image', 'category_id': 'chairs',
            'price': '1.00', 'stock': '1', 'image': (io.BytesIO(b'not an image'), 'fake.png'),
        })
        self.assertEqual(response.status_code, 400)

    def test_replaced_and_removed_product_images_are_deleted(self):
        token = self.login()
        def image(color):
            data = io.BytesIO()
            Image.new('RGB', (2, 2), color).save(data, 'PNG')
            data.seek(0)
            return (data, 'image.png')
        data = {'csrf_token': token, 'name': 'Image test', 'description': 'Test image',
                'category_id': 'chairs', 'price': '2.00', 'stock': '12'}
        self.client.post('/admin/products/new', data={**data, 'image': image('red')})
        with app.db() as con:
            product = con.execute("SELECT id,image_url FROM products WHERE name='Image test'").fetchone()
        old_image = app.UPLOAD_PATH / product['image_url'].rsplit('/', 1)[1]
        self.assertTrue(old_image.exists())
        response = self.client.post(f"/admin/products/{product['id']}/edit",
                                    data={**data, 'image': image('blue')})
        self.assertEqual(response.status_code, 302)
        with app.db() as con:
            current_url = con.execute('SELECT image_url FROM products WHERE id=?', (product['id'],)).fetchone()['image_url']
        new_image = app.UPLOAD_PATH / current_url.rsplit('/', 1)[1]
        self.assertFalse(old_image.exists())
        self.assertTrue(new_image.exists())
        self.assertEqual(self.client.get(product['image_url']).status_code, 404)
        self.client.post(f"/admin/products/{product['id']}/delete", data={'csrf_token': token})
        self.assertFalse(new_image.exists())
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM image_deletions').fetchone()[0], 0)

    def test_shared_product_image_is_retained_until_last_active_product_is_removed(self):
        token = self.login()
        filename = 'a' * 32 + '.png'
        image_url = '/uploads/' + filename
        app.UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
        (app.UPLOAD_PATH / filename).write_bytes(b'image')
        with app.db() as con:
            con.execute("UPDATE products SET image_url=? WHERE id IN ('chiavari','folding_chairs')", (image_url,))
            shared = con.execute("SELECT COUNT(*) FROM products WHERE active=1 AND image_url=?", (image_url,)).fetchone()[0]
        self.assertEqual(shared, 2)
        self.client.post('/admin/products/chiavari/delete', data={'csrf_token': token})
        self.assertTrue((app.UPLOAD_PATH / filename).exists())
        self.client.post('/admin/products/folding_chairs/delete', data={'csrf_token': token})
        self.assertFalse((app.UPLOAD_PATH / filename).exists())

    def test_failed_blob_deletion_is_reported_and_can_be_retried(self):
        token = self.login()
        filename = 'b' * 32 + '.png'
        image_url = '/uploads/' + filename
        with app.db() as con:
            con.execute("UPDATE products SET image_url=? WHERE id='chiavari'", (image_url,))
        container = Mock()
        container.delete_blob.side_effect = app.AzureError('storage unavailable')
        with patch.object(app, 'get_image_container', return_value=container):
            response = self.client.post('/admin/products/chiavari/delete', data={'csrf_token': token},
                                        follow_redirects=True)
            self.assertIn(b'could not be deleted', response.data)
            self.assertIn(b'Retry image deletion', response.data)
            with app.db() as con:
                self.assertEqual(con.execute('SELECT image_url FROM image_deletions').fetchone()['image_url'], image_url)
            container.delete_blob.assert_called_once_with(filename, delete_snapshots='include')
            container.delete_blob.side_effect = None
            response = self.client.post('/admin/images/delete-retry', data={'csrf_token': token}, follow_redirects=True)
            self.assertIn(b'Pending product image deletions completed', response.data)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM image_deletions').fetchone()[0], 0)

    def test_external_product_image_is_not_deleted(self):
        token = self.login()
        with app.db() as con:
            con.execute("UPDATE products SET image_url='https://example.com/image.png' WHERE id='chiavari'")
        with patch.object(app, 'get_image_container') as storage:
            self.client.post('/admin/products/chiavari/delete', data={'csrf_token': token})
            storage.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM image_deletions').fetchone()[0], 0)

    def test_postgres_connection_translates_sqlite_write_syntax(self):
        connection = Mock()
        postgres = app.PostgresConnection(connection)
        postgres.execute('BEGIN IMMEDIATE')
        connection.execute.assert_called_once_with('SELECT pg_advisory_xact_lock(581234901)')
        connection.reset_mock()
        parameters = ('booking', 'paid')
        postgres.execute(
            'INSERT OR IGNORE INTO email_outbox(booking_id,kind) VALUES (?,?)', parameters
        )
        connection.execute.assert_called_once_with(
            'INSERT INTO email_outbox(booking_id,kind) VALUES (%s,%s) ON CONFLICT DO NOTHING',
            parameters,
        )

    def test_azure_blob_storage_uploads_and_serves_product_images(self):
        token = self.login()
        with app.db() as con:
            category_id = con.execute('SELECT id FROM categories LIMIT 1').fetchone()['id']
        image = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(image, 'PNG')
        image.seek(0)
        container = Mock()
        blob = Mock()
        blob.readall.return_value = b'blob-image'
        container.get_blob_client.return_value.download_blob.return_value = blob
        with patch.object(app, 'get_image_container', return_value=container):
            response = self.client.post('/admin/products/new', data={
                'csrf_token': token, 'name': 'Blob table', 'description': 'Stored remotely',
                'category_id': category_id, 'price': '2.75', 'stock': '12', 'image': (image, 'image.png'),
            })
            self.assertEqual(response.status_code, 302)
            with app.db() as con:
                product = con.execute("SELECT * FROM products WHERE name='Blob table'").fetchone()
            image_response = self.client.get(product['image_url'])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response.data, b'blob-image')
        self.assertEqual(image_response.mimetype, 'image/png')
        upload = container.upload_blob.call_args.kwargs
        self.assertEqual(upload['name'], product['image_url'].rsplit('/', 1)[1])
        self.assertEqual(upload['content_settings'].content_type, 'image/png')

    def test_daily_checkout_and_verified_webhook(self):
        with patch.object(app.stripe, 'StripeClient') as client:
            create = client.return_value.v1.checkout.sessions.create
            create.return_value = SimpleNamespace(id='cs_123', url='https://checkout.stripe.test/session')
            response = self.booking(payment_option='deposit')
        self.assertEqual(response.status_code, 303)
        args = create.call_args.args[0]
        self.assertEqual(args['line_items'][0]['price_data']['unit_amount'], 630)
        self.assertTrue(args['success_url'].startswith('https://rental.example/booking/'))
        with app.db() as con:
            booking = con.execute('SELECT * FROM bookings').fetchone()
        self.assertEqual(booking['subtotal_pence'], 2100)
        event = {'id': 'evt_123', 'type': 'checkout.session.completed', 'data': {'object': {
            'id': 'cs_123', 'payment_status': 'paid', 'currency': 'gbp',
            'client_reference_id': booking['id'], 'amount_total': 630,
        }}}
        payload = json.dumps(event).encode()
        timestamp = int(time.time())
        signature = hmac.new(b'whsec_example', str(timestamp).encode() + b'.' + payload, hashlib.sha256).hexdigest()
        headers = {'Stripe-Signature': f't={timestamp},v1={signature}'}
        self.assertEqual(self.client.post('/stripe/webhook', data=payload, headers={'Stripe-Signature': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.post('/stripe/webhook', data=payload, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/stripe/webhook', data=payload, headers=headers).status_code, 200)
        self.assertEqual(self.emailjs.call_count, 1)
        request_payload = json.loads(self.emailjs.call_args.args[0].data)
        self.assertIn('/returns/', request_payload['template_params']['email_message'])
        with app.db() as con:
            paid = con.execute('SELECT * FROM bookings').fetchone()
        self.assertEqual(paid['paid_pence'], 630)
        self.assertEqual(paid['status'], 'deposit_paid_pending_confirmation')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        pickup_midnight = datetime.fromisoformat(booking['start_date']).replace(tzinfo=ZoneInfo('Europe/London'))
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            stripe_client = factory.return_value
            stripe_client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
            stripe_client.v1.payment_intents.retrieve.return_value = SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge={'amount_refunded': 0, 'disputed': False})
            stripe_client.v1.payment_intents.create.return_value = SimpleNamespace(id='pi_balance', status='requires_confirmation')
            stripe_client.v1.payment_intents.confirm.return_value = SimpleNamespace(id='pi_balance', status='succeeded', amount_received=1470)
            balance_worker.run_once(pickup_midnight)
            balance_worker.run_once(pickup_midnight)
            stripe_client.v1.payment_intents.create.assert_called_once()
            self.assertEqual(stripe_client.v1.payment_intents.create.call_args.args[0]['amount'], 1470)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT paid_pence FROM bookings').fetchone()[0], 2100)
            self.assertEqual(con.execute('SELECT status FROM balance_payments').fetchone()[0], 'paid')
        self.login()
        self.assertIn(b'Test User', self.client.get('/admin').data)

    def test_quote_does_not_reserve_and_overlap_does(self):
        self.assertEqual(self.booking(payment_option='quote', qty_chiavari='100').status_code, 302)
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_1', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(qty_chiavari='100').status_code, 303)
        self.assertEqual(self.booking(qty_chiavari='1').status_code, 400)

    def test_unavailable_payment_does_not_hold_stock(self):
        with patch.dict(os.environ, {'STRIPE_SECRET_KEY': ''}):
            self.assertEqual(self.booking().status_code, 400)
        with patch.dict(os.environ, {'STRIPE_WEBHOOK_SECRET': ''}):
            self.assertEqual(self.booking().status_code, 400)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0], 0)

    def test_same_day_deposit_is_rejected_without_reservation(self):
        today = date(2030, 3, 10)
        with patch.object(app, 'business_today', return_value=today):
            response = self.booking(payment_option='deposit', start_date=today.isoformat(), end_date=today.isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'Same-day hires require payment in full', response.data)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0], 0)

    def test_deposit_requires_authorization_and_sets_future_usage(self):
        self.assertEqual(self.booking(payment_option='deposit', balance_consent='').status_code, 400)
        with patch.object(app.stripe, 'StripeClient') as client:
            create = client.return_value.v1.checkout.sessions.create
            create.return_value = SimpleNamespace(id='cs_deposit', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(payment_option='deposit').status_code, 303)
            self.assertNotIn('payment_method_types', create.call_args.args[0])
            self.assertEqual(create.call_args.args[0]['payment_intent_data']['setup_future_usage'], 'off_session')
            self.assertEqual(create.call_args.args[0]['customer_creation'], 'always')

    def test_multiple_product_dates_pricing_and_email(self):
        today = app.business_today()
        start = today + timedelta(days=3)
        override = today + timedelta(days=1)
        with patch.object(app.stripe, 'StripeClient') as client:
            create = client.return_value.v1.checkout.sessions.create
            create.return_value = SimpleNamespace(id='cs_dates', url='https://checkout.stripe.test')
            response = self.booking(start_date=start.isoformat(), end_date=(start + timedelta(days=2)).isoformat(),
                                    qty_chiavari='2', qty_banquet_chairs='3', price_banquet_chairs='400',
                                    different_dates_banquet_chairs='yes', start_date_banquet_chairs=override.isoformat(),
                                    end_date_banquet_chairs=(override + timedelta(days=1)).isoformat())
            self.assertEqual(response.status_code, 303)
            self.assertEqual(create.call_args.args[0]['line_items'][0]['price_data']['unit_amount'], 4500)
        with app.db() as con:
            booking = con.execute('SELECT * FROM bookings').fetchone()
            items = con.execute('SELECT * FROM booking_items ORDER BY product_id').fetchall()
            self.assertEqual((booking['start_date'], booking['end_date']), (override.isoformat(), (start + timedelta(days=2)).isoformat()))
            self.assertEqual((items[0]['start_date'], items[0]['end_date']), (override.isoformat(), (override + timedelta(days=1)).isoformat()))
            self.assertEqual((items[1]['start_date'], items[1]['end_date']), (start.isoformat(), (start + timedelta(days=2)).isoformat()))
            con.execute("INSERT INTO email_outbox(booking_id,kind) VALUES (?, 'paid')", (booking['id'],))
        with app.app.test_request_context('/'):
            app.send_booking_email(booking['id'], 'paid')
            body = json.loads(self.emailjs.call_args.args[0].data)['template_params']['email_message']
            self.assertIn(f"{override.isoformat()} to {(override + timedelta(days=1)).isoformat()}", body)
            self.assertIn(f"{start.isoformat()} to {(start + timedelta(days=2)).isoformat()}", body)

    def test_mixed_dates_availability_and_invalid_overrides(self):
        today = app.business_today()
        start = today + timedelta(days=3)
        other = today + timedelta(days=1)
        self.insert_paid('reserved', other, other, 100)
        data = dict(start_date=start.isoformat(), end_date=start.isoformat(), qty_chiavari='1',
                    qty_banquet_chairs='1', price_banquet_chairs='400', different_dates_chiavari='yes',
                    start_date_chiavari=other.isoformat(), end_date_chiavari=other.isoformat())
        self.assertEqual(self.booking(**data).status_code, 400)
        data['start_date_chiavari'] = start.isoformat()
        data['end_date_chiavari'] = start.isoformat()
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_other', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(**data).status_code, 303)
        data['end_date_chiavari'] = other.isoformat()
        self.assertEqual(self.booking(**data).status_code, 400)

    def test_availability_uses_date_ranges_overdue_and_returns(self):
        today = app.business_today()
        self.insert_paid('overdue_stock', today - timedelta(days=2), today - timedelta(days=1), 10)
        self.insert_paid('future_stock', today + timedelta(days=3), today + timedelta(days=4), 20)
        query = {'product': 'chiavari', 'start': (today + timedelta(days=3)).isoformat(),
                 'end': (today + timedelta(days=4)).isoformat()}
        response = self.client.get('/availability', query_string=query)
        self.assertEqual(response.json, {'available': 70})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with app.db() as con:
            con.execute("UPDATE booking_items SET returned=5 WHERE booking_id='overdue_stock'")
        self.assertEqual(self.client.get('/availability', query_string=query).json, {'available': 75})
        query['start'] = (today + timedelta(days=5)).isoformat()
        query['end'] = query['start']
        self.assertEqual(self.client.get('/availability', query_string=query).json, {'available': 95})
        query['end'] = 'not-a-date'
        self.assertEqual(self.client.get('/availability', query_string=query).status_code, 400)
        query.update(end=query['start'], product='missing')
        self.assertEqual(self.client.get('/availability', query_string=query).status_code, 404)

    def test_catalog_availability_uses_today_or_selected_dates_and_returns(self):
        today = app.business_today()
        future = today + timedelta(days=3)
        self.insert_paid('catalog_overdue', today - timedelta(days=2), today - timedelta(days=1), 10)
        self.insert_paid('catalog_future', future, future, 20)
        response = self.client.get('/availability/catalog')
        self.assertEqual(response.json['start'], today.isoformat())
        self.assertEqual(response.json['end'], today.isoformat())
        self.assertEqual(response.json['available']['chiavari'], 90)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn(b'Total available today: 90', self.client.get('/').data)
        query = {'start': future.isoformat(), 'end': future.isoformat()}
        self.assertEqual(self.client.get('/availability/catalog', query_string=query).json['available']['chiavari'], 70)
        with app.db() as con:
            con.execute("UPDATE booking_items SET returned=5 WHERE booking_id='catalog_overdue'")
        self.assertEqual(self.client.get('/availability/catalog', query_string=query).json['available']['chiavari'], 75)
        self.assertEqual(self.client.get('/availability/catalog', query_string={'start': future.isoformat()}).status_code, 400)
        self.assertEqual(self.client.get('/availability/catalog', query_string={'start': 'invalid', 'end': 'invalid'}).status_code, 400)

    def test_admin_bookings_table_links_to_existing_actions(self):
        booking_id = 'a' * 32
        self.insert_paid(booking_id, date.today(), date.today(), 2)
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation'
                WHERE id=?""", (booking_id,))
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES (?,'2029-01-01','scheduled','2029-01-01')""", (booking_id,))
        self.login()
        response = self.client.get('/admin')
        self.assertIn(b'<table class="bookings-table">', response.data)
        self.assertIn(f'href="#booking-{booking_id}"'.encode(), response.data)
        self.assertIn(b'href="mailto:test%40example.com"', response.data)
        self.assertIn(b'data-label="Customer"', response.data)
        self.assertIn(b'data-label="Status"', response.data)
        self.assertIn(b'data-label="Paid"', response.data)
        self.assertIn(f'id="booking-{booking_id}"'.encode(), response.data)
        self.assertIn(f'/admin/bookings/{booking_id}/balance/stop'.encode(), response.data)
        self.assertIn(f'/admin/bookings/{booking_id}/returns/chiavari'.encode(), response.data)

    def test_mixed_dates_deposit_uses_earliest_selected_product(self):
        today = app.business_today()
        default = today + timedelta(days=3)
        data = dict(payment_option='deposit', start_date=default.isoformat(), end_date=default.isoformat(),
                    qty_banquet_chairs='1', price_banquet_chairs='400', different_dates_banquet_chairs='yes',
                    start_date_banquet_chairs=today.isoformat(), end_date_banquet_chairs=today.isoformat())
        self.assertEqual(self.booking(**data).status_code, 400)
        tomorrow = today + timedelta(days=1)
        data.update(start_date_banquet_chairs=tomorrow.isoformat(), end_date_banquet_chairs=tomorrow.isoformat())
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_early', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(**data).status_code, 303)
        with app.db() as con:
            booking = con.execute('SELECT start_date,subtotal_pence,due_pence FROM bookings').fetchone()
            self.assertEqual((booking['start_date'], booking['subtotal_pence'], booking['due_pence']), (tomorrow.isoformat(), 1100, 330))

    def test_return_request_checks_item_start_date(self):
        today = app.business_today()
        self.insert_paid('return_dates', today, today + timedelta(days=5))
        with app.db() as con:
            con.execute("UPDATE booking_items SET start_date=? WHERE booking_id='return_dates'", ((today + timedelta(days=3)).isoformat(),))
        token = app.return_token('return_dates')
        csrf = self.csrf(self.client.get('/returns/' + token))
        response = self.client.post('/returns/' + token, data={'csrf_token': csrf, 'product_id': 'chiavari'}, follow_redirects=True)
        self.assertIn(b'cannot be requested before', response.data)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM return_events WHERE booking_id='return_dates'").fetchone()[0], 0)

    def test_balance_worker_charges_once_after_nine(self):
        from datetime import datetime, timezone
        self.insert_paid('balance', date(2030, 1, 5), date(2030, 1, 5), 2)
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='balance'")
            con.execute("INSERT INTO balance_payments(booking_id,consent_at,status,created_at) VALUES ('balance','2029-01-01','scheduled','2029-01-01')")
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = factory.return_value
            client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
            client.v1.payment_intents.retrieve.return_value = SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge={'amount_refunded': 0, 'disputed': False})
            client.v1.payment_intents.create.return_value = SimpleNamespace(id='pi_balance', status='requires_confirmation')
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(id='pi_balance', status='succeeded', amount_received=700)
            balance_worker.run_once(datetime(2030, 1, 5, 8, 59, tzinfo=timezone.utc))
            self.assertEqual(client.v1.payment_intents.create.call_count, 0)
            balance_worker.run_once(datetime(2030, 1, 5, 9, 0, tzinfo=timezone.utc))
            balance_worker.run_once(datetime(2030, 1, 5, 9, 1, tzinfo=timezone.utc))
            self.assertEqual(client.v1.payment_intents.create.call_count, 1)
            self.assertEqual(client.v1.payment_intents.create.call_args.args[0]['amount'], 700)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='balance'").fetchone()[0], 1000)
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='balance'").fetchone()[0], 'paid')

    def test_failed_balance_cancels_intent_and_sends_recovery_link(self):
        from datetime import datetime, timezone
        self.insert_paid('failedbalance', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='failedbalance'")
            con.execute("INSERT INTO balance_payments(booking_id,consent_at,status,created_at) VALUES ('failedbalance','2029-01-01','scheduled','2029-01-01')")
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = factory.return_value
            client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
            client.v1.payment_intents.retrieve.return_value = SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge={'amount_refunded': 0, 'disputed': False})
            client.v1.payment_intents.create.return_value = SimpleNamespace(id='pi_failed', status='requires_confirmation')
            client.v1.payment_intents.confirm.return_value = SimpleNamespace(id='pi_failed', status='requires_action')
            client.v1.payment_intents.cancel.return_value = SimpleNamespace(id='pi_failed', status='canceled')
            balance_worker.run_once(datetime(2030, 1, 5, 10, 0, tzinfo=timezone.utc))
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='failedbalance'").fetchone()[0], 'needs_manual')
            self.assertEqual(con.execute("SELECT status FROM email_outbox WHERE booking_id='failedbalance' AND kind='balance_due'").fetchone()[0], 'sent')

    def test_balance_recovery_checkout_and_webhook_are_idempotent(self):
        self.insert_paid('recover', date.today(), date.today())
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300 WHERE id='recover'")
            con.execute("INSERT INTO balance_payments(booking_id,status,created_at) VALUES ('recover','needs_manual','2026-01-01')")
        token = app.URLSafeSerializer(app.app.secret_key, salt='balance-payment-v1').dumps('recover')
        page = self.client.get('/balance/' + token)
        csrf = self.csrf(page)
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_recover', url='https://checkout.stripe.test', status='open')
            self.assertEqual(self.client.post('/balance/' + token, data={'csrf_token': csrf}).status_code, 303)
        event = {'id': 'evt_recover', 'type': 'checkout.session.completed', 'data': {'object': {
            'id': 'cs_recover', 'payment_status': 'paid', 'currency': 'gbp', 'amount_total': 700,
            'client_reference_id': 'recover', 'metadata': {'purpose': 'hire_balance'}}}}
        with patch.object(app.stripe.Webhook, 'construct_event', return_value=event):
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='recover'").fetchone()[0], 1000)
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='recover'").fetchone()[0], 'paid')

    def test_old_balance_requires_manual_payment_and_no_automatic_attempt(self):
        from datetime import datetime, timezone
        self.insert_paid('olddeposit', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300 WHERE id='olddeposit'")
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            balance_worker.run_once(datetime(2030, 1, 5, 10, 0, tzinfo=timezone.utc))
            factory.return_value.v1.payment_intents.create.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='olddeposit'").fetchone()[0], 'needs_manual')

    def test_admin_stops_only_scheduled_balances(self):
        self.insert_paid('stopbalance', date.today(), date.today())
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',
                subtotal_pence=1000,paid_pence=300 WHERE id='stopbalance'""")
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('stopbalance','2029-01-01','scheduled','2029-01-01')""")
        token = self.csrf(self.client.get('/admin/login'))
        self.assertEqual(self.client.post('/admin/bookings/stopbalance/balance/stop',
                                          data={'csrf_token': token}).status_code, 302)
        csrf = self.login()
        self.assertEqual(self.client.post('/admin/bookings/stopbalance/balance/stop').status_code, 400)
        self.assertEqual(self.client.post('/admin/bookings/stopbalance/balance/stop',
                                          data={'csrf_token': csrf}).status_code, 303)
        client = Mock()
        balance_worker.process_booking('stopbalance', client)
        client.v1.payment_intents.create.assert_not_called()
        with app.db() as con:
            row = con.execute("""SELECT b.status,b.paid_pence,p.status AS balance_status
                FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id='stopbalance'""").fetchone()
            self.assertEqual((row['status'], row['paid_pence'], row['balance_status']),
                             ('deposit_paid_pending_confirmation', 300, 'stopped'))
        for status in ('creating', 'confirming', 'processing', 'paid', 'needs_manual'):
            with self.subTest(status=status):
                with app.db() as con:
                    con.execute("UPDATE balance_payments SET status=? WHERE booking_id='stopbalance'", (status,))
                self.client.post('/admin/bookings/stopbalance/balance/stop', data={'csrf_token': csrf})
                with app.db() as con:
                    self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='stopbalance'").fetchone()[0], status)

    def test_refunded_disputed_or_unverified_deposit_requires_review(self):
        self.insert_paid('refundcheck', date.today(), date.today())
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',
                stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='refundcheck'""")
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('refundcheck','2029-01-01','scheduled','2029-01-01')""")
        for charge in ({'amount_refunded': 1, 'disputed': False},
                       {'amount_refunded': 0, 'disputed': True}, None, {'amount_refunded': 0}):
            with self.subTest(charge=charge):
                with app.db() as con:
                    con.execute("UPDATE balance_payments SET status='scheduled' WHERE booking_id='refundcheck'")
                client = Mock()
                client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
                client.v1.payment_intents.retrieve.return_value = SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge=charge)
                balance_worker.process_booking('refundcheck', client)
                client.v1.payment_intents.create.assert_not_called()
                with app.db() as con:
                    self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='refundcheck'").fetchone()[0], 'needs_review')

    def test_missing_saved_card_requires_manual_payment_without_charge(self):
        from datetime import datetime, timezone
        self.insert_paid('missingcard', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='missingcard'")
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('missingcard','2029-01-01','scheduled','2029-01-01')""")
        with patch.object(balance_worker.stripe, 'StripeClient') as factory:
            client = factory.return_value
            client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer=None, payment_intent='pi_deposit')
            balance_worker.run_once(datetime(2030, 1, 5, 10, 0, tzinfo=timezone.utc))
            client.v1.payment_intents.create.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='missingcard'").fetchone()[0], 'needs_manual')
            self.assertEqual(con.execute("SELECT status FROM email_outbox WHERE booking_id='missingcard' AND kind='balance_due'").fetchone()[0], 'sent')

    def test_failed_balance_email_waits_before_retry(self):
        from datetime import datetime, timezone
        self.insert_paid('emailretry', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300 WHERE id='emailretry'")
            con.execute("INSERT INTO balance_payments(booking_id,status,created_at) VALUES ('emailretry','needs_manual','2029-01-01')")
            con.execute("INSERT INTO email_outbox(booking_id,kind,status,claimed_at) VALUES ('emailretry','balance_due','failed',?)", (datetime.now(timezone.utc).isoformat(),))
        with patch.object(app, 'send_booking_email') as send:
            balance_worker.run_once(datetime(2030, 1, 5, 10, 0, tzinfo=timezone.utc))
            send.assert_not_called()
            with app.db() as con:
                con.execute("UPDATE email_outbox SET claimed_at=? WHERE booking_id='emailretry'",
                            ((datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),))
            balance_worker.run_once(datetime(2030, 1, 5, 10, 1, tzinfo=timezone.utc))
            send.assert_called_once_with('emailretry', 'balance_due')

    def test_admin_rechecks_only_verified_matching_balance_payment(self):
        self.insert_paid('review', date.today(), date.today())
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300 WHERE id='review'")
            con.execute("""INSERT INTO balance_payments(booking_id,status,created_at,intent_id)
                VALUES ('review','needs_review','2026-01-01','pi_review')""")
        csrf = self.login()
        with patch.object(app.stripe, 'StripeClient') as factory:
            retrieve = factory.return_value.v1.payment_intents.retrieve
            payment = SimpleNamespace(id='pi_review', metadata={'booking_id': 'review', 'purpose': 'hire_balance'},
                                      currency='gbp', amount_received=699, status='succeeded')
            retrieve.return_value = payment
            self.client.post('/admin/bookings/review/balance/recheck', data={'csrf_token': csrf})
            with app.db() as con:
                self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='review'").fetchone()[0], 300)
            payment.amount_received = 700
            self.client.post('/admin/bookings/review/balance/recheck', data={'csrf_token': csrf})
            self.client.post('/admin/bookings/review/balance/recheck', data={'csrf_token': csrf})
        with app.db() as con:
            self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='review'").fetchone()[0], 1000)
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='review'").fetchone()[0], 'paid')

    def test_admin_recheck_recovery_requires_matching_paid_session(self):
        self.insert_paid('review2', date.today(), date.today())
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300 WHERE id='review2'")
            con.execute("""INSERT INTO balance_payments(booking_id,status,created_at,checkout_id)
                VALUES ('review2','needs_review','2026-01-01','cs_review')""")
        csrf = self.login()
        with patch.object(app.stripe, 'StripeClient') as factory:
            session = SimpleNamespace(id='cs_review', client_reference_id='review2', metadata={'purpose':'hire_balance'},
                                      currency='gbp', amount_total=700, payment_status='unpaid')
            factory.return_value.v1.checkout.sessions.retrieve.return_value = session
            self.client.post('/admin/bookings/review2/balance/recheck', data={'csrf_token': csrf})
            with app.db() as con:
                self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='review2'").fetchone()[0], 300)
            session.payment_status = 'paid'
            self.client.post('/admin/bookings/review2/balance/recheck', data={'csrf_token': csrf})
        with app.db() as con:
            self.assertEqual(con.execute("SELECT paid_pence FROM bookings WHERE id='review2'").fetchone()[0], 1000)

    def test_worker_skips_when_another_worker_holds_lock(self):
        lock_path = app.DB_PATH.with_name(app.DB_PATH.name + '.balance-lock')
        with closing(sqlite3.connect(lock_path)) as lock:
            lock.execute('BEGIN EXCLUSIVE')
            with patch.object(balance_worker, 'collect_due') as collect:
                balance_worker.run_once()
                collect.assert_not_called()

    def test_postgres_worker_requires_advisory_lock(self):
        with patch.dict(os.environ, {'BALANCE_COLLECTION_ENABLED': '1'}), \
                patch.object(app, 'DATABASE_URL', 'postgresql://example/database'), \
                patch.object(balance_worker.psycopg, 'connect') as connect, \
                patch.object(balance_worker, 'collect_due') as collect, \
                patch.object(balance_worker, 'record_heartbeat') as heartbeat:
            lock = connect.return_value.__enter__.return_value
            lock.execute.return_value.fetchone.return_value = (False,)
            balance_worker.run_once()
            collect.assert_not_called()
            heartbeat.assert_not_called()
            lock.execute.return_value.fetchone.return_value = (True,)
            balance_worker.run_once()
            collect.assert_called_once()
            heartbeat.assert_called_once()

    def test_worker_does_not_confirm_an_intent_after_claim_is_lost(self):
        self.insert_paid('lostclaim', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',
                stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='lostclaim'""")
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('lostclaim','2029-01-01','scheduled','2029-01-01')""")
        client = Mock()
        client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
        client.v1.payment_intents.retrieve.return_value = SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge={'amount_refunded': 0, 'disputed': False})
        def lose_claim(*args, **kwargs):
            with app.db() as con:
                con.execute("UPDATE balance_payments SET status='needs_review' WHERE booking_id='lostclaim'")
            return SimpleNamespace(id='pi_unclaimed', status='requires_confirmation')
        client.v1.payment_intents.create.side_effect = lose_claim
        balance_worker.process_booking('lostclaim', client)
        client.v1.payment_intents.confirm.assert_not_called()
        with app.db() as con:
            row = con.execute("SELECT status,intent_id FROM balance_payments WHERE booking_id='lostclaim'").fetchone()
            self.assertEqual((row['status'], row['intent_id']), ('needs_review', None))

    def test_worker_reconciles_confirmation_after_connection_failure(self):
        self.insert_paid('uncertainconfirm', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',
                stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='uncertainconfirm'""")
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('uncertainconfirm','2029-01-01','scheduled','2029-01-01')""")
        client = Mock()
        client.v1.checkout.sessions.retrieve.return_value = SimpleNamespace(customer='cus_test', payment_intent='pi_deposit')
        client.v1.payment_intents.retrieve.side_effect = [
            SimpleNamespace(status='succeeded', payment_method='pm_test', latest_charge={'amount_refunded': 0, 'disputed': False}),
            SimpleNamespace(id='pi_balance', status='succeeded', amount_received=700),
        ]
        client.v1.payment_intents.create.return_value = SimpleNamespace(id='pi_balance', status='requires_confirmation')
        client.v1.payment_intents.confirm.side_effect = app.stripe.APIConnectionError('simulated timeout')
        with self.assertRaises(app.stripe.APIConnectionError):
            balance_worker.process_booking('uncertainconfirm', client)
        balance_worker.process_booking('uncertainconfirm', client)
        client.v1.payment_intents.create.assert_called_once()
        client.v1.payment_intents.confirm.assert_called_once()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='uncertainconfirm'").fetchone()[0], 'paid')

    def test_worker_does_not_retry_uncertain_creation_after_idempotency_window(self):
        from datetime import datetime, timezone
        self.insert_paid('oldcreation', date(2030, 1, 5), date(2030, 1, 5))
        with app.db() as con:
            con.execute("""UPDATE bookings SET payment_option='deposit',status='deposit_paid_pending_confirmation',
                stripe_session_id='cs_deposit',subtotal_pence=1000,paid_pence=300 WHERE id='oldcreation'""")
            started = (datetime.now(timezone.utc) - timedelta(hours=21)).isoformat()
            con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                VALUES ('oldcreation','2029-01-01','creating',?)""", (started,))
        client = Mock()
        balance_worker.process_booking('oldcreation', client)
        client.v1.payment_intents.create.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='oldcreation'").fetchone()[0], 'needs_review')

    def test_worker_due_time_respects_london_daylight_saving(self):
        from datetime import datetime, timezone
        self.assertFalse(balance_worker.due('2030-01-05', datetime(2030, 1, 5, 8, 59, tzinfo=timezone.utc)))
        self.assertTrue(balance_worker.due('2030-01-05', datetime(2030, 1, 5, 9, 0, tzinfo=timezone.utc)))
        self.assertFalse(balance_worker.due('2030-07-05', datetime(2030, 7, 5, 7, 59, tzinfo=timezone.utc)))
        self.assertTrue(balance_worker.due('2030-07-05', datetime(2030, 7, 5, 8, 0, tzinfo=timezone.utc)))
        self.assertFalse(balance_worker.due('2030-01-05', datetime(2030, 1, 4, 23, 59, tzinfo=timezone.utc), midnight=True))
        self.assertTrue(balance_worker.due('2030-01-05', datetime(2030, 1, 5, 0, 0, tzinfo=timezone.utc), midnight=True))
        self.assertFalse(balance_worker.due('2030-07-05', datetime(2030, 7, 4, 22, 59, tzinfo=timezone.utc), midnight=True))
        self.assertTrue(balance_worker.due('2030-07-05', datetime(2030, 7, 4, 23, 0, tzinfo=timezone.utc), midnight=True))

    def test_worker_preserves_earlier_consent_schedule(self):
        from datetime import datetime, timezone
        for booking_id in ('midnight', 'legacy'):
            self.insert_paid(booking_id, date(2030, 1, 5), date(2030, 1, 5))
            with app.db() as con:
                con.execute("""UPDATE bookings SET payment_option='deposit',
                    status='deposit_paid_pending_confirmation',subtotal_pence=1000,paid_pence=300,
                    terms_version=? WHERE id=?""",
                    (app.TERMS_VERSION if booking_id == 'midnight' else None, booking_id))
                con.execute("""INSERT INTO balance_payments(booking_id,consent_at,status,created_at)
                    VALUES (?,'2029-01-01','scheduled','2029-01-01')""", (booking_id,))
        with patch.object(balance_worker, 'process_booking') as process:
            balance_worker.run_once(datetime(2030, 1, 5, 0, 0, tzinfo=timezone.utc))
            self.assertEqual([call.args[0] for call in process.call_args_list], ['midnight'])
            process.reset_mock()
            balance_worker.run_once(datetime(2030, 1, 5, 9, 0, tzinfo=timezone.utc))
            self.assertCountEqual([call.args[0] for call in process.call_args_list], ['midnight', 'legacy'])

    def test_disabled_balance_collection_hides_and_rejects_deposits(self):
        with patch.dict(os.environ, {'BALANCE_COLLECTION_ENABLED': '0'}):
            page = self.client.get('/')
            self.assertIn(b'temporarily unavailable', page.data)
            self.assertEqual(self.booking(payment_option='deposit').status_code, 400)
            with patch.object(balance_worker, 'collect_due') as collect:
                balance_worker.run_once()
                collect.assert_not_called()

    def test_recovery_reuses_saved_session_and_avoids_expired_idempotency_key(self):
        self.insert_paid('recover2', date.today(), date.today())
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',subtotal_pence=1000,paid_pence=300 WHERE id='recover2'")
            con.execute("""INSERT INTO balance_payments(booking_id,status,created_at,checkout_id,checkout_started_at)
                VALUES ('recover2','needs_manual','2026-01-01','cs_saved','2020-01-01T00:00:00+00:00')""")
        token = app.URLSafeSerializer(app.app.secret_key, salt='balance-payment-v1').dumps('recover2')
        csrf = self.csrf(self.client.get('/balance/' + token))
        with patch.object(app.stripe, 'StripeClient') as client:
            sessions = client.return_value.v1.checkout.sessions
            sessions.retrieve.return_value = SimpleNamespace(id='cs_saved', status='open', url='https://checkout.stripe.test')
            self.assertEqual(self.client.post('/balance/' + token, data={'csrf_token': csrf}).status_code, 303)
            sessions.create.assert_not_called()
            sessions.retrieve.return_value = SimpleNamespace(id='cs_saved', status='complete', url=None)
            result = self.client.post('/balance/' + token, data={'csrf_token': csrf})
            self.assertIn(b'being reconciled', result.data)
            sessions.retrieve.return_value = SimpleNamespace(id='cs_saved', status='expired', url=None)
            self.client.post('/balance/' + token, data={'csrf_token': csrf})
            sessions.create.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='recover2'").fetchone()[0], 'needs_review')

    def test_recovery_unknown_creation_is_not_retried_after_twenty_hours(self):
        self.insert_paid('unknown', date.today(), date.today())
        with app.db() as con:
            con.execute("UPDATE bookings SET payment_option='deposit',subtotal_pence=1000,paid_pence=300 WHERE id='unknown'")
            con.execute("""INSERT INTO balance_payments(booking_id,status,created_at,checkout_started_at)
                VALUES ('unknown','needs_manual','2026-01-01','2020-01-01T00:00:00+00:00')""")
        token = app.URLSafeSerializer(app.app.secret_key, salt='balance-payment-v1').dumps('unknown')
        csrf = self.csrf(self.client.get('/balance/' + token))
        with patch.object(app.stripe, 'StripeClient') as client:
            self.assertEqual(self.client.post('/balance/' + token, data={'csrf_token': csrf}).status_code, 303)
            client.assert_not_called()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM balance_payments WHERE booking_id='unknown'").fetchone()[0], 'needs_review')

    def test_async_payment_reserves_stock_until_success_or_failure(self):
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_async', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(qty_chiavari='100').status_code, 303)
        with app.db() as con:
            booking = con.execute('SELECT * FROM bookings').fetchone()
            con.execute("UPDATE bookings SET created_at='2020-01-01' WHERE id=?", (booking['id'],))
        payment = {'id': 'cs_async', 'payment_status': 'unpaid', 'currency': 'gbp',
                   'client_reference_id': booking['id'], 'amount_total': booking['due_pence']}
        event = {'id': 'evt_pending', 'type': 'checkout.session.completed', 'data': {'object': payment}}
        with patch.object(app.stripe.Webhook, 'construct_event', return_value=event):
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)
        self.assertEqual(self.booking(qty_chiavari='1').status_code, 400)
        event['id'] = 'evt_failed'
        event['type'] = 'checkout.session.async_payment_failed'
        with patch.object(app.stripe.Webhook, 'construct_event', return_value=event):
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT status FROM bookings WHERE id=?', (booking['id'],)).fetchone()[0], 'payment_failed')
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_next', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(qty_chiavari='100').status_code, 303)
        event['id'] = 'evt_late_paid'
        event['type'] = 'checkout.session.async_payment_succeeded'
        payment['payment_status'] = 'paid'
        with patch.object(app.stripe.Webhook, 'construct_event', return_value=event):
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT status FROM bookings WHERE id=?', (booking['id'],)).fetchone()[0], 'paid_pending_confirmation')
        self.login()
        self.assertIn(b'Stock conflict', self.client.get('/admin').data)

    def test_webhook_retries_if_session_not_stored_yet(self):
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_race', url='https://checkout.stripe.test')
            self.assertEqual(self.booking().status_code, 303)
        with app.db() as con:
            booking = con.execute('SELECT * FROM bookings').fetchone()
            con.execute('UPDATE bookings SET stripe_session_id=NULL WHERE id=?', (booking['id'],))
        event = {'id': 'evt_race', 'type': 'checkout.session.completed', 'data': {'object': {
            'id': 'cs_race', 'payment_status': 'paid', 'currency': 'gbp',
            'client_reference_id': booking['id'], 'amount_total': booking['due_pence']}}}
        with patch.object(app.stripe.Webhook, 'construct_event', return_value=event):
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 409)
            with app.db() as con:
                con.execute("UPDATE bookings SET stripe_session_id='cs_race' WHERE id=?", (booking['id'],))
            self.assertEqual(self.client.post('/stripe/webhook').status_code, 200)

    def test_changed_price_requires_cart_review(self):
        with app.db() as con:
            con.execute("UPDATE products SET price_pence=500 WHERE id='chiavari'")
        response = self.booking()
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'has changed', response.data)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0], 0)

    def insert_paid(self, booking_id, start, end, quantity=10):
        item = {'id': 'chiavari', 'name': 'Chiavari Chairs', 'quantity': quantity, 'price_pence': 350}
        with app.db() as con:
            con.execute('''INSERT INTO bookings
                (id,created_at,name,email,phone,start_date,end_date,items_json,subtotal_pence,
                 payment_option,due_pence,paid_pence,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (booking_id, '2020-01-01', 'Test', 'test@example.com', '', start.isoformat(),
                 end.isoformat(), json.dumps([item]), 3500, 'full', 3500, 3500, 'paid_pending_confirmation'))
            app.insert_booking_items(con, booking_id, [item])

    def test_return_request_partial_confirmation_and_overdue_conflict(self):
        today = date.today()
        self.insert_paid('overdue', today - timedelta(days=3), today - timedelta(days=1), 100)
        self.insert_paid('future', today + timedelta(days=1), today + timedelta(days=2), 2)
        self.assertEqual(self.booking(qty_chiavari='1').status_code, 400)
        token = app.return_token('overdue')
        self.assertEqual(self.client.get('/returns/invalid').status_code, 404)
        response = self.client.get('/returns/' + token)
        csrf = self.csrf(response)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.post('/returns/' + token, data={'product_id': 'chiavari'}).status_code, 400)
        self.assertEqual(self.client.post('/returns/' + token, data={'csrf_token': csrf, 'product_id': 'chiavari'}).status_code, 303)
        self.assertEqual(self.client.post('/returns/' + token, data={'csrf_token': csrf, 'product_id': 'chiavari'}).status_code, 303)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM return_events').fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT returned FROM booking_items WHERE booking_id='overdue'").fetchone()[0], 0)
        admin_csrf = self.login()
        dashboard = self.client.get('/admin')
        self.assertIn(b'Stock conflict', dashboard.data)
        response = self.client.post('/admin/bookings/overdue/returns/chiavari',
                                    data={'csrf_token': admin_csrf, 'quantity': '3'})
        self.assertEqual(response.status_code, 303)
        with app.db() as con:
            item = con.execute("SELECT * FROM booking_items WHERE booking_id='overdue'").fetchone()
        self.assertEqual(item['returned'], 3)
        self.assertEqual(item['return_requested'], 1)
        self.assertNotIn(b'Stock conflict', self.client.get('/admin').data)
        self.assertEqual(self.booking(qty_chiavari='2').status_code, 400)
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_new', url='https://checkout.stripe.test')
            self.assertEqual(self.booking(qty_chiavari='1').status_code, 303)
        self.client.post('/admin/bookings/overdue/returns/chiavari', data={'csrf_token': admin_csrf, 'quantity': '97'})
        with app.db() as con:
            item = con.execute("SELECT * FROM booking_items WHERE booking_id='overdue'").fetchone()
        self.assertEqual((item['returned'], item['return_requested']), (100, 0))
        self.assertEqual(self.client.post('/admin/bookings/overdue/returns/chiavari', data={'csrf_token': admin_csrf, 'quantity': '1'}).status_code, 303)

    def test_future_nonoverlapping_reservations_use_peak_not_sum(self):
        today = date.today()
        self.insert_paid('first', today + timedelta(days=1), today + timedelta(days=1), 60)
        self.insert_paid('second', today + timedelta(days=2), today + timedelta(days=2), 60)
        with patch.object(app.stripe, 'StripeClient') as client:
            client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(id='cs_peak', url='https://checkout.stripe.test')
            response = self.booking(qty_chiavari='40', end_date=(today + timedelta(days=2)).isoformat())
        self.assertEqual(response.status_code, 303)

    def test_email_content_success_failure_and_retry(self):
        self.assertEqual(self.booking(payment_option='quote').status_code, 302)
        quote_payload = json.loads(self.emailjs.call_args.args[0].data)
        quote = quote_payload['template_params']
        self.assertIn('quote request', quote['email_message'])
        self.assertNotIn('/returns/', quote['email_message'])
        self.assertEqual(quote['to_email'], 'test@example.com')
        self.insert_paid('paid', date.today(), date.today(), 2)
        with app.db() as con:
            con.execute("INSERT INTO email_outbox(booking_id,kind) VALUES ('paid','paid')")
        self.emailjs.side_effect = OSError('temporary')
        with app.app.test_request_context('/'):
            app.send_booking_email('paid', 'paid')
        with app.db() as con:
            outbox = con.execute("SELECT status,last_error FROM email_outbox WHERE booking_id='paid'").fetchone()
            self.assertEqual((outbox['status'], outbox['last_error']), ('failed', 'OSError'))
        self.emailjs.side_effect = None
        admin_csrf = self.login()
        self.client.post('/admin/bookings/paid/email/paid/retry', data={'csrf_token': admin_csrf})
        request_payload = json.loads(self.emailjs.call_args.args[0].data)
        message = request_payload['template_params']['email_message']
        self.assertIn('/returns/', message)
        self.assertIn('£35.00', message)
        self.assertEqual(request_payload['service_id'], 'service_test')
        self.assertEqual(request_payload['template_id'], 'template_test')
        self.assertEqual(request_payload['user_id'], 'public_test')
        self.assertEqual(request_payload['accessToken'], 'private_test')
        with app.db() as con:
            self.assertEqual(con.execute("SELECT status FROM email_outbox WHERE booking_id='paid'").fetchone()[0], 'sent')

    def test_email_http_failure_records_status_without_provider_response(self):
        self.emailjs.side_effect = HTTPError(
            'https://api.emailjs.com', 403, 'private provider detail', None, io.BytesIO(b'private response'))
        self.assertEqual(self.booking(payment_option='quote').status_code, 302)
        with app.db() as con:
            outbox = con.execute("SELECT status,last_error FROM email_outbox WHERE kind='quote'").fetchone()
        self.assertEqual((outbox['status'], outbox['last_error']), ('failed', 'HTTP 403'))

    def test_email_http_failure_identifies_non_browser_block(self):
        self.emailjs.side_effect = HTTPError(
            'https://api.emailjs.com', 403, 'Forbidden', None,
            io.BytesIO(b'API calls are disabled for non-browser applications'))
        self.assertEqual(self.booking(payment_option='quote').status_code, 302)
        with app.db() as con:
            outbox = con.execute("SELECT status,last_error FROM email_outbox WHERE kind='quote'").fetchone()
        self.assertEqual(outbox['last_error'], 'HTTP 403: non-browser API calls disabled')

    def test_return_security_and_stock_edit_guard(self):
        today = date.today()
        self.insert_paid('reserved', today, today + timedelta(days=1), 80)
        token = app.return_token('reserved')
        self.assertEqual(self.client.get('/returns/' + token).status_code, 200)
        with app.db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM return_events').fetchone()[0], 0)
        self.assertEqual(self.client.post('/admin/bookings/reserved/returns/chiavari', data={'quantity': '1'}).status_code, 400)
        admin_csrf = self.login()
        response = self.client.post('/admin/products/chiavari/edit', data={
            'csrf_token': admin_csrf, 'name': 'Chair', 'description': 'Chair description',
            'category_id': 'chairs', 'price': '3.50', 'stock': '79',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'maximum already reserved', response.data)
        with app.db() as con:
            self.assertEqual(con.execute("SELECT stock FROM products WHERE id='chiavari'").fetchone()[0], 100)


if __name__ == '__main__':
    unittest.main()
