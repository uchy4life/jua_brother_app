"""Jua Brothers demo booking backend. Configure Stripe and business rules before launch."""
import json
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import stripe
import psycopg
from psycopg.rows import dict_row
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, send_file, send_from_directory, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer
from PIL import Image, UnidentifiedImageError

load_dotenv()
app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    SECRET_KEY=os.getenv('SECRET_KEY'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE') == '1',
)
app.jinja_env.filters['fromjson'] = json.loads
DB_PATH = Path(os.getenv('DATABASE_PATH', str(Path(__file__).parent / 'bookings.db')))
UPLOAD_PATH = Path(os.getenv('UPLOAD_PATH', str(Path(__file__).parent / 'uploads')))
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
AZURE_STORAGE_ACCOUNT_URL = os.getenv('AZURE_STORAGE_ACCOUNT_URL', '').strip()
AZURE_STORAGE_CONTAINER = os.getenv('AZURE_STORAGE_CONTAINER', 'product-images')
if os.getenv('WEBSITE_INSTANCE_ID') and (not DATABASE_URL or not AZURE_STORAGE_ACCOUNT_URL):
    raise RuntimeError('App Service requires DATABASE_URL and AZURE_STORAGE_ACCOUNT_URL')
DEPOSIT_PERCENT = 30
TERMS_VERSION = '2026-11-24'
CHECKOUT_HOLD_MINUTES = 35
_blob_container_client = None


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        if statement.strip().upper() == 'BEGIN IMMEDIATE':
            # Preserve SQLite's single-writer semantics for inventory transactions.
            return self.connection.execute('SELECT pg_advisory_xact_lock(581234901)')
        ignored_insert = re.match(r'\s*INSERT\s+OR\s+IGNORE\s+INTO\b', statement, re.IGNORECASE)
        if ignored_insert:
            statement = statement[:ignored_insert.start()] + re.sub(
                r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', statement[ignored_insert.start():],
                count=1, flags=re.IGNORECASE)
            statement = statement.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        return self.connection.execute(statement.replace('?', '%s'), parameters)


def business_today():
    return datetime.now(ZoneInfo('Europe/London')).date()


@contextmanager
def db():
    if DATABASE_URL:
        connection = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        try:
            with connection:
                yield PostgresConnection(connection)
        finally:
            connection.close()
        return
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db():
    if not DATABASE_URL:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('''CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
            email TEXT NOT NULL, phone TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            items_json TEXT NOT NULL, subtotal_pence INTEGER NOT NULL,
            payment_option TEXT NOT NULL, due_pence INTEGER NOT NULL,
            paid_pence INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            stripe_session_id TEXT UNIQUE
        )''')
        booking_columns = table_columns(con, 'bookings')
        for column in ('terms_version', 'terms_accepted_at'):
            if column not in booking_columns:
                con.execute(f'ALTER TABLE bookings ADD COLUMN {column} TEXT')
        con.execute('''CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY)''')
        category_name_constraint = '' if DATABASE_URL else ' UNIQUE COLLATE NOCASE'
        con.execute(f'''CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY, name TEXT NOT NULL{category_name_constraint}
        )''')
        if DATABASE_URL:
            con.execute('CREATE UNIQUE INDEX IF NOT EXISTS categories_name_lower ON categories (lower(name))')
        con.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, category_id TEXT NOT NULL REFERENCES categories(id),
            name TEXT NOT NULL, description TEXT NOT NULL, price_pence INTEGER NOT NULL,
            stock INTEGER NOT NULL, image_url TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS image_deletions (
            image_url TEXT PRIMARY KEY, created_at TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)''')
        con.execute('''CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT NOT NULL, created_at TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS booking_items (
            booking_id TEXT NOT NULL REFERENCES bookings(id),
            product_id TEXT NOT NULL, name TEXT NOT NULL, quantity INTEGER NOT NULL,
            returned INTEGER NOT NULL DEFAULT 0, return_requested INTEGER NOT NULL DEFAULT 0,
            start_date TEXT, end_date TEXT,
            PRIMARY KEY (booking_id, product_id)
        )''')
        item_columns = table_columns(con, 'booking_items')
        for column in ('start_date', 'end_date'):
            if column not in item_columns:
                con.execute(f'ALTER TABLE booking_items ADD COLUMN {column} TEXT')
        con.execute('''UPDATE booking_items SET
            start_date=COALESCE(start_date,(SELECT start_date FROM bookings WHERE id=booking_id)),
            end_date=COALESCE(end_date,(SELECT end_date FROM bookings WHERE id=booking_id))
            WHERE start_date IS NULL OR end_date IS NULL''')
        return_event_id = 'BIGSERIAL PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY'
        con.execute(f'''CREATE TABLE IF NOT EXISTS return_events (
            id {return_event_id}, booking_id TEXT NOT NULL, product_id TEXT NOT NULL,
            quantity INTEGER NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS email_outbox (
            booking_id TEXT NOT NULL REFERENCES bookings(id), kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, sent_at TEXT, claimed_at TEXT, PRIMARY KEY (booking_id, kind)
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS email_rate_limits (
            provider TEXT PRIMARY KEY, next_allowed_at TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS worker_heartbeats (
            name TEXT PRIMARY KEY, completed_at TEXT NOT NULL
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS balance_payments (
            booking_id TEXT PRIMARY KEY REFERENCES bookings(id),
            consent_at TEXT, status TEXT NOT NULL DEFAULT 'awaiting_deposit',
            intent_id TEXT, checkout_id TEXT, last_error TEXT,
            created_at TEXT NOT NULL, checkout_started_at TEXT
        )''')
        columns = table_columns(con, 'balance_payments')
        if 'checkout_started_at' not in columns:
            con.execute('ALTER TABLE balance_payments ADD COLUMN checkout_started_at TEXT')
        if not con.execute("SELECT 1 FROM migrations WHERE name='booking_items_v1'").fetchone():
            for booking in con.execute('SELECT id,items_json,status FROM bookings').fetchall():
                insert_booking_items(con, booking['id'], json.loads(booking['items_json']))
                kind = 'quote' if booking['status'] == 'quote_requested' else 'paid' if booking['status'] in ('paid_pending_confirmation', 'deposit_paid_pending_confirmation') else None
                if kind:
                    con.execute('INSERT OR IGNORE INTO email_outbox(booking_id,kind) VALUES (?,?)', (booking['id'], kind))
            con.execute("INSERT INTO migrations(name) VALUES ('booking_items_v1')")
        if not DATABASE_URL and not con.execute("SELECT 1 FROM migrations WHERE name='initial_catalog'").fetchone():
            seed = json.loads((Path(__file__).parent / 'initial_catalog.json').read_text(encoding='utf-8'))
            for category in seed['categories']:
                con.execute('INSERT INTO categories(id, name) VALUES (?, ?)', (category['id'], category['name']))
            for product in seed['products']:
                con.execute('''INSERT INTO products
                    (id,category_id,name,description,price_pence,stock,image_url)
                    VALUES (?,?,?,?,?,?,?)''',
                    tuple(product[key] for key in
                          ('id', 'category_id', 'name', 'description', 'price_pence', 'stock', 'image_url')))
            con.execute("INSERT INTO migrations(name) VALUES ('initial_catalog')")


def table_columns(con, table_name):
    if DATABASE_URL:
        rows = con.execute('''SELECT column_name AS name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=?''', (table_name,)).fetchall()
    else:
        rows = con.execute(f'PRAGMA table_info({table_name})').fetchall()
    return {row['name'] for row in rows}


def get_image_container():
    global _blob_container_client
    if not AZURE_STORAGE_ACCOUNT_URL:
        return None
    if _blob_container_client is None:
        service = BlobServiceClient(
            account_url=AZURE_STORAGE_ACCOUNT_URL,
            credential=DefaultAzureCredential(),
        )
        _blob_container_client = service.get_container_client(AZURE_STORAGE_CONTAINER)
    return _blob_container_client


def store_product_image(upload, filename, content_type):
    container = get_image_container()
    upload.stream.seek(0)
    if container:
        container.upload_blob(
            name=filename,
            data=upload.stream,
            overwrite=False,
            content_settings=ContentSettings(content_type=content_type),
        )
        return
    UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    upload.save(UPLOAD_PATH / filename)


def managed_image_filename(image_url):
    match = re.fullmatch(r'/uploads/([0-9a-f]{32}\.(?:jpg|png|webp))', image_url or '')
    return match.group(1) if match else None


def queue_image_deletion(con, image_url):
    if managed_image_filename(image_url):
        con.execute('INSERT OR IGNORE INTO image_deletions(image_url,created_at) VALUES (?,?)',
                    (image_url, datetime.now(timezone.utc).isoformat()))


def delete_queued_image(image_url):
    filename = managed_image_filename(image_url)
    if not filename:
        raise ValueError('Invalid queued product image')
    try:
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            if not con.execute('SELECT 1 FROM image_deletions WHERE image_url=?', (image_url,)).fetchone():
                return True
            if not con.execute('SELECT 1 FROM products WHERE active=1 AND image_url=?', (image_url,)).fetchone():
                container = get_image_container()
                if container:
                    try:
                        container.delete_blob(filename, delete_snapshots='include')
                    except ResourceNotFoundError:
                        pass
                else:
                    (UPLOAD_PATH / filename).unlink(missing_ok=True)
            con.execute('DELETE FROM image_deletions WHERE image_url=?', (image_url,))
        return True
    except (AzureError, OSError):
        app.logger.exception('Could not delete replaced or removed product image %s', filename)
        return False


def insert_booking_items(con, booking_id, items):
    booking = con.execute('SELECT start_date,end_date FROM bookings WHERE id=?', (booking_id,)).fetchone()
    for item in items:
        con.execute('''INSERT INTO booking_items(booking_id,product_id,name,quantity,start_date,end_date)
            VALUES (?,?,?,?,?,?)''', (booking_id, item['id'], item['name'], item['quantity'],
                                      item.get('start_date', booking['start_date']),
                                      item.get('end_date', booking['end_date'])))


init_db()


@app.get('/healthz')
def health_check():
    with db() as con:
        con.execute('SELECT 1')
    return {'status': 'ok'}, 200


@app.get('/healthz/balance-worker')
def balance_worker_health():
    if os.getenv('BALANCE_COLLECTION_ENABLED') != '1':
        return {'status': 'disabled'}, 503
    if not balance_worker_ready():
        return {'status': 'unavailable'}, 503
    return {'status': 'ok'}, 200


def balance_worker_ready():
    if os.getenv('BALANCE_COLLECTION_ENABLED') != '1':
        return False
    with db() as con:
        row = con.execute("SELECT completed_at FROM worker_heartbeats WHERE name='balance'").fetchone()
    if not row:
        return False
    try:
        completed_at = datetime.fromisoformat(row['completed_at'])
        if completed_at.tzinfo is None:
            raise ValueError('Heartbeat has no timezone')
    except ValueError:
        app.logger.error('Invalid balance worker heartbeat')
        return False
    return timedelta(0) <= datetime.now(timezone.utc) - completed_at <= timedelta(minutes=10)


PAID_STATUSES = ('paid_pending_confirmation', 'deposit_paid_pending_confirmation')


def reservation_rows(con, today, exclude_id=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=CHECKOUT_HOLD_MINUTES)).isoformat()
    return con.execute('''SELECT b.id,i.start_date,i.end_date,b.status,i.product_id,
        i.quantity,i.returned FROM bookings b JOIN booking_items i ON i.booking_id=b.id
        WHERE (b.status IN (?,?,'payment_processing') OR (b.status='awaiting_payment' AND b.created_at>=?))
        AND b.id != ? AND i.returned < i.quantity''',
        (*PAID_STATUSES, cutoff, exclude_id or '')).fetchall()


def peak_reserved(rows, product_id, start, end, today):
    changes = {}
    for row in rows:
        if row['product_id'] != product_id:
            continue
        row_start = date.fromisoformat(row['start_date'])
        row_end = date.fromisoformat(row['end_date'])
        if row['status'] in (*PAID_STATUSES, 'payment_processing') and row_end < today:
            row_end = date.max
        first, last = max(start, row_start), min(end, row_end)
        if first > last:
            continue
        remaining = row['quantity'] - row['returned']
        changes[first.toordinal()] = changes.get(first.toordinal(), 0) + remaining
        changes[last.toordinal() + 1] = changes.get(last.toordinal() + 1, 0) - remaining
    active = peak = 0
    for day in sorted(changes):
        active += changes[day]
        peak = max(peak, active)
    return peak


def return_token(booking_id):
    if not app.secret_key:
        raise RuntimeError('SECRET_KEY is required for customer return links')
    return URLSafeSerializer(app.secret_key, salt='booking-return-v1').dumps(booking_id)


def wait_for_emailjs_rate_limit():
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        now = datetime.now(timezone.utc)
        row = con.execute(
            "SELECT next_allowed_at FROM email_rate_limits WHERE provider='emailjs'"
        ).fetchone()
        available_at = max(now, datetime.fromisoformat(row['next_allowed_at'])) if row else now
        next_allowed_at = available_at + timedelta(seconds=1)
        con.execute('''INSERT INTO email_rate_limits(provider,next_allowed_at) VALUES ('emailjs',?)
            ON CONFLICT(provider) DO UPDATE SET next_allowed_at=excluded.next_allowed_at''',
            (next_allowed_at.isoformat(),))
    delay = (available_at - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        time.sleep(delay)


def send_booking_email(booking_id, kind):
    with db() as con:
        now = datetime.now(timezone.utc)
        claim = con.execute('''UPDATE email_outbox SET status='sending',claimed_at=?
            WHERE booking_id=? AND kind=? AND (status IN ('pending','failed')
              OR (status='sending' AND (claimed_at IS NULL OR claimed_at<?)))''',
            (now.isoformat(), booking_id, kind, (now - timedelta(minutes=5)).isoformat()))
        if not claim.rowcount:
            return
        booking = con.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
    try:
        service_id = os.environ['EMAILJS_SERVICE_ID']
        template_id = os.environ['EMAILJS_TEMPLATE_ID']
        public_key = os.environ['EMAILJS_PUBLIC_KEY']
        private_key = os.environ['EMAILJS_PRIVATE_KEY']
        public_base = os.environ['PUBLIC_BASE_URL'].rstrip('/')
        if not public_base.startswith(('https://', 'http://')):
            raise ValueError('PUBLIC_BASE_URL must be an absolute HTTP(S) URL')
        lines = [f"Hello {booking['name']},", '',
                 'Your quote request has been received.' if kind == 'quote' else 'Your balance needs payment.' if kind == 'balance_due' else 'Your payment has been received. Your hire is pending staff confirmation.',
                 f"Booking reference: {booking['id'][:12]}",
                 f"Hire dates: {booking['start_date']} to {booking['end_date']}", '']
        for item in json.loads(booking['items_json']):
            lines.append(f"- {item['name']}: {item['quantity']} at £{item['price_pence'] / 100:.2f} per item per day"
                         f" ({item.get('start_date', booking['start_date'])} to {item.get('end_date', booking['end_date'])})")
        lines.extend(['', f"Total: £{booking['subtotal_pence'] / 100:.2f}",
                      f"Paid: £{booking['paid_pence'] / 100:.2f}",
                      f"Balance: £{(booking['subtotal_pence'] - booking['paid_pence']) / 100:.2f}"])
        if kind == 'balance_due':
            token = URLSafeSerializer(app.secret_key, salt='balance-payment-v1').dumps(booking_id)
            lines.extend(['', 'Pay your outstanding balance securely:',
                          public_base + url_for('balance_payment', token=token),
                          'Do not share this private link.'])
        elif kind == 'paid':
            lines.extend(['', 'After returning the items, request confirmation using this private link:',
                          public_base + url_for('customer_return', token=return_token(booking_id)),
                          'Do not share this link. Items become available only after staff confirm receipt.'])
        else:
            lines.extend(['', 'A quote request does not reserve stock. Please contact us to follow up.'])
        subject = ('Jua Brothers balance payment' if kind == 'balance_due'
                   else 'Jua Brothers booking information' if kind == 'paid'
                   else 'Jua Brothers quote request')
        payload = {
            'service_id': service_id,
            'template_id': template_id,
            'user_id': public_key,
            'accessToken': private_key,
            'template_params': {
                'to_email': booking['email'],
                'email_subject': subject,
                'email_message': '\n'.join(lines),
            },
        }
        wait_for_emailjs_rate_limit()
        email_request = Request(
            'https://api.emailjs.com/api/v1.0/email/send',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urlopen(email_request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f'EmailJS returned HTTP {response.status}')
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        error = f'HTTP {exc.code}' if isinstance(exc, HTTPError) else type(exc).__name__
        if isinstance(exc, HTTPError) and exc.code == 403:
            response_body = exc.read(16384).decode('utf-8', errors='replace').lower()
            if re.search(r'error(?:\s+code)?\s*:?\s*1010\b', response_body):
                error = 'HTTP 403: Cloudflare error 1010'
            elif 'api calls are disabled for non-browser applications' in response_body:
                error = 'HTTP 403: non-browser API calls disabled'
            elif 'api calls in strict mode, but no private key was passed' in response_body:
                error = 'HTTP 403: private key missing'
        app.logger.error('Booking email failed for %s (%s): %s', booking_id, kind, error)
        with db() as con:
            con.execute('''UPDATE email_outbox SET status='failed',attempts=attempts+1,last_error=?
                WHERE booking_id=? AND kind=?''', (error, booking_id, kind))
        return
    with db() as con:
        con.execute('''UPDATE email_outbox SET status='sent',attempts=attempts+1,last_error=NULL,sent_at=?
            WHERE booking_id=? AND kind=?''', (datetime.now(timezone.utc).isoformat(), booking_id, kind))


def catalog(con=None):
    if con is None:
        with db() as connection:
            return catalog(connection)
    rows = con.execute('''SELECT p.*, c.name AS category_name FROM products p
        JOIN categories c ON c.id=p.category_id WHERE p.active=1 ORDER BY c.name, p.name''')
    return [dict(row) for row in rows]


def available_quantities(products, reservations, start, end, today):
    return {product['id']: max(0, product['stock'] - peak_reserved(
        reservations, product['id'], start, end, today)) for product in products}


def storefront(error=None):
    today = business_today()
    with db() as con:
        products = catalog(con)
        available = available_quantities(products, reservation_rows(con, today), today, today, today)
    for product in products:
        product['available_today'] = available[product['id']]
    return render_template('index.html', catalog=products, today=today.isoformat(),
                           error=error, deposit_enabled=balance_worker_ready(),
                           terms_version=TERMS_VERSION,
                           has_logo=(Path(app.static_folder) / 'logo.png').is_file())


@app.get('/')
def home():
    return storefront()


@app.get('/availability/catalog')
def catalog_availability():
    today = business_today()
    start_text, end_text = request.args.get('start'), request.args.get('end')
    if start_text is None and end_text is None:
        start = end = today
    else:
        try:
            start = date.fromisoformat(start_text or '')
            end = date.fromisoformat(end_text or '')
        except ValueError:
            return {'error': 'Please enter valid hire dates.'}, 400
        if start < today or end < start:
            return {'error': 'Please enter valid hire dates.'}, 400
    with db() as con:
        products = catalog(con)
        available = available_quantities(products, reservation_rows(con, today), start, end, today)
    return {'available': available, 'start': start.isoformat(), 'end': end.isoformat()}, 200, {'Cache-Control': 'no-store'}


@app.get('/availability')
def availability():
    try:
        start = date.fromisoformat(request.args['start'])
        end = date.fromisoformat(request.args['end'])
    except (KeyError, ValueError):
        return {'error': 'Please enter valid hire dates.'}, 400
    today = business_today()
    if start < today or end < start:
        return {'error': 'Please enter valid hire dates.'}, 400
    product_id = request.args.get('product', '')
    with db() as con:
        product = con.execute('SELECT id,stock FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
        if not product:
            return {'error': 'This product is no longer available.'}, 404
        reserved = peak_reserved(reservation_rows(con, today), product_id, start, end, today)
        remaining = max(0, product['stock'] - reserved)
    return {'available': remaining}, 200, {'Cache-Control': 'no-store'}


def invalid(message):
    return storefront(message), 400


@app.post('/book')
def create_booking():
    name = request.form.get('customer_name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    option = request.form.get('payment_option', '')
    cart_token = request.form.get('cart_token', '')
    if cart_token and not re.fullmatch(r'[a-f0-9]{32}', cart_token):
        return invalid('Unable to process the cart. Please refresh and try again.')
    if not name or len(name) > 120 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email)>254 or len(phone)>40:
        return invalid('Please enter a valid name, email and phone number.')
    if option not in ('full', 'deposit', 'quote'):
        return invalid('Please select a payment option.')
    if request.form.get('terms_accepted') != TERMS_VERSION:
        return invalid('Please read and accept the current terms and conditions.')
    try:
        start = date.fromisoformat(request.form['start_date'])
        end = date.fromisoformat(request.form['end_date'])
    except (KeyError, ValueError):
        return invalid('Please enter valid hire dates.')
    today = business_today()
    if start < today or end < start:
        return invalid('The hire dates must be in the future and the end cannot precede the start.')
    if option == 'deposit' and not balance_worker_ready():
        return invalid('Deposit payments are temporarily unavailable. Please select full payment or request a quote.')
    if option == 'deposit' and request.form.get('balance_consent') != 'yes':
        return invalid('Please authorize the remaining balance payment or select another payment option.')
    if option != 'quote' and (not os.getenv('STRIPE_SECRET_KEY') or not os.getenv('STRIPE_WEBHOOK_SECRET')):
        app.logger.error('Stripe checkout is unavailable: API key or webhook signing secret is missing')
        return invalid('Online checkout is not configured yet. Please contact us for a quote.')
    booking_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    status = 'quote_requested' if option == 'quote' else 'awaiting_payment'
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        products = catalog(con)
        known_ids = {product['id'] for product in products}
        for key in request.form:
            if key.startswith('qty_') and key[4:] not in known_ids and request.form[key] != '0':
                return invalid('A product in your cart is no longer available. Please refresh the page.')
        items = []
        for product in products:
            raw = request.form.get('qty_' + product['id'], '0')
            if not re.fullmatch(r'\d{1,5}', raw):
                return invalid('Please enter whole-number quantities between 0 and 10,000.')
            quantity = int(raw)
            if quantity > product['stock']:
                return invalid(f"Only {product['stock']} units of {product['name']} are owned. Please reduce your quantity.")
            if quantity:
                if request.form.get('price_' + product['id']) != str(product['price_pence']):
                    return invalid(f"The price of {product['name']} has changed. Please review your cart and try again.")
                different = request.form.get('different_dates_' + product['id'], '')
                if different not in ('', 'yes'):
                    return invalid('Please select valid product hire dates.')
                if different == 'yes':
                    try:
                        item_start = date.fromisoformat(request.form['start_date_' + product['id']])
                        item_end = date.fromisoformat(request.form['end_date_' + product['id']])
                    except (KeyError, ValueError):
                        return invalid(f"Please enter valid hire dates for {product['name']}.")
                    if item_start < today or item_end < item_start:
                        return invalid(f"Please enter valid hire dates for {product['name']}.")
                else:
                    item_start, item_end = start, end
                items.append({**product, 'quantity': quantity,
                              'start_date': item_start.isoformat(), 'end_date': item_end.isoformat()})
        if not items:
            return invalid('Select at least one product.')
        first_start = min(date.fromisoformat(item['start_date']) for item in items)
        last_end = max(date.fromisoformat(item['end_date']) for item in items)
        if option == 'deposit' and first_start == today:
            return invalid('Same-day hires require payment in full. Please select full payment.')
        subtotal = sum(item['price_pence'] * item['quantity'] *
                       ((date.fromisoformat(item['end_date']) - date.fromisoformat(item['start_date'])).days + 1)
                       for item in items)
        due = 0 if option == 'quote' else (subtotal * DEPOSIT_PERCENT + 50) // 100 if option == 'deposit' else subtotal
        if option != 'quote':
            reservations = reservation_rows(con, today)
            for item in items:
                reserved = peak_reserved(reservations, item['id'], date.fromisoformat(item['start_date']),
                                         date.fromisoformat(item['end_date']), today)
                remaining = max(0, item['stock'] - reserved)
                if item['quantity'] > remaining:
                    return invalid(f"Only {remaining} {item['name']} available for those dates. Choose fewer or request a quote.")
        con.execute('''INSERT INTO bookings
            (id,created_at,name,email,phone,start_date,end_date,items_json,subtotal_pence,payment_option,due_pence,status,terms_version,terms_accepted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (booking_id,created_at.isoformat(),name,email,phone,first_start.isoformat(),last_end.isoformat(),json.dumps(items),subtotal,option,due,status,TERMS_VERSION,created_at.isoformat()))
        insert_booking_items(con, booking_id, items)
        if option == 'deposit':
            con.execute('INSERT INTO balance_payments(booking_id,consent_at,created_at) VALUES (?,?,?)',
                        (booking_id, created_at.isoformat(), created_at.isoformat()))
        if option == 'quote':
            con.execute("INSERT INTO email_outbox(booking_id,kind) VALUES (?, 'quote')", (booking_id,))
    if option == 'quote':
        send_booking_email(booking_id, 'quote')
        return redirect(url_for('booking_status', booking_id=booking_id, checkout='quote',
                                cart_token=cart_token) if cart_token else url_for('booking_status', booking_id=booking_id))
    status_path = url_for('booking_status', booking_id=booking_id)
    public_base = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
    status_url = public_base + status_path if public_base else url_for('booking_status', booking_id=booking_id, _external=True)
    try:
        stripe_client = stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])
        params = {
            'mode':'payment',
            'customer_email':email,
            'client_reference_id':booking_id,
            'line_items':[{'price_data': {'currency':'gbp', 'unit_amount':due,
                         'product_data': {'name': 'Jua Brothers hire — ' + ('30% deposit' if option == 'deposit' else 'full payment')}},
                         'quantity':1}],
            'metadata':{'booking_id':booking_id},
            'expires_at':int((created_at + timedelta(minutes=CHECKOUT_HOLD_MINUTES)).timestamp()),
            'success_url':status_url + '?checkout=returned' + ('&cart_token=' + cart_token if cart_token else ''),
            'cancel_url':status_url + '?checkout=cancelled',
        }
        if option == 'deposit':
            params.update({'customer_creation': 'always',
                           'payment_intent_data': {'setup_future_usage': 'off_session'}})
        checkout_session = stripe_client.v1.checkout.sessions.create(params, options={'idempotency_key': 'booking-' + booking_id})
    except stripe.StripeError:
        app.logger.exception('Stripe checkout session creation failed')
        with db() as con:
            con.execute("UPDATE bookings SET status='payment_failed' WHERE id=?", (booking_id,))
        return invalid('Payment checkout is temporarily unavailable. Please try again later.')
    with db() as con:
        con.execute('UPDATE bookings SET stripe_session_id=? WHERE id=?', (checkout_session.id, booking_id))
    return redirect(checkout_session.url, code=303)


@app.get('/booking/<booking_id>')
def booking_status(booking_id):
    with db() as con:
        row = con.execute('SELECT id, payment_option, status, subtotal_pence, due_pence, paid_pence FROM bookings WHERE id=?', (booking_id,)).fetchone()
        email = con.execute('SELECT status FROM email_outbox WHERE booking_id=?', (booking_id,)).fetchone()
        items = con.execute('SELECT name,quantity,start_date,end_date FROM booking_items WHERE booking_id=?', (booking_id,)).fetchall()
    if row is None:
        abort(404)
    # Booking IDs are unguessable, but do not show private contact data on this public page.
    token = request.args.get('cart_token', '')
    checkout = request.args.get('checkout', '')
    clear_cart_token = token if (re.fullmatch(r'[a-f0-9]{32}', token) and
        ((checkout == 'quote' and row['status'] == 'quote_requested') or
         (checkout == 'returned' and row['payment_option'] != 'quote' and row['status'] != 'payment_failed'))) else None
    return render_template('status.html', booking=row, items=items, email_status=email['status'] if email else None,
                           clear_cart_token=clear_cart_token)


@app.post('/stripe/webhook')
def stripe_webhook():
    secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    if not secret:
        abort(503)
    try:
        event = stripe.Webhook.construct_event(request.get_data(), request.headers.get('Stripe-Signature', ''), secret)
    except (ValueError, stripe.SignatureVerificationError):
        abort(400)
    event_type = event['type']
    if event_type not in ('checkout.session.completed', 'checkout.session.async_payment_succeeded',
                          'checkout.session.async_payment_failed', 'checkout.session.expired'):
        return '', 200
    checkout_session = event['data']['object']
    booking_id = checkout_session.get('client_reference_id')
    if checkout_session.get('metadata', {}).get('purpose') == 'hire_balance':
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('''SELECT b.subtotal_pence,b.paid_pence,p.checkout_id,p.status
                FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
            if not row or row['checkout_id'] != checkout_session.get('id'):
                abort(409)
            if checkout_session.get('currency') != 'gbp' or (row['status'] != 'paid' and checkout_session.get('amount_total') != row['subtotal_pence'] - row['paid_pence']):
                abort(400)
            if con.execute('SELECT 1 FROM stripe_events WHERE id=?', (event['id'],)).fetchone():
                return '', 200
            con.execute('INSERT INTO stripe_events(id) VALUES (?)', (event['id'],))
            if checkout_session.get('payment_status') == 'paid' and row['status'] != 'paid':
                con.execute('UPDATE bookings SET paid_pence=subtotal_pence WHERE id=?', (booking_id,))
                con.execute("UPDATE balance_payments SET status='paid',last_error=NULL WHERE booking_id=?", (booking_id,))
            elif event_type in ('checkout.session.expired', 'checkout.session.async_payment_failed') and row['status'] != 'paid':
                con.execute("UPDATE balance_payments SET status='needs_review',last_error='Recovery checkout expired or failed; contact Stripe before retry' WHERE booking_id=?", (booking_id,))
        return '', 200
    result = record_booking_checkout(checkout_session, event_type, event['id'])
    if result == 'unmatched':
        abort(400)
    if result == 'retry':
        abort(409)
    return '', 200


def record_booking_checkout(checkout_session, event_type, event_id=None):
    booking_id = checkout_session.get('client_reference_id')
    should_send_email = False
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        booking = con.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
        if booking and not booking['stripe_session_id']:
            app.logger.warning('Stripe event arrived before session was saved for booking %s; retrying', booking_id)
            return 'retry'
        if (not booking or booking['stripe_session_id'] != checkout_session.get('id')
                or booking['due_pence'] != checkout_session.get('amount_total')
                or checkout_session.get('currency') != 'gbp'):
            app.logger.error('Unmatched Stripe payment session %s', checkout_session.get('id'))
            return 'unmatched'
        if event_id:
            if con.execute('SELECT 1 FROM stripe_events WHERE id=?', (event_id,)).fetchone():
                return 'recorded'
            con.execute('INSERT INTO stripe_events(id) VALUES (?)', (event_id,))
        if checkout_session.get('payment_status') == 'paid':
            if booking['status'] not in PAID_STATUSES:
                con.execute('UPDATE bookings SET paid_pence=?, status=? WHERE id=?',
                            (booking['due_pence'], 'deposit_paid_pending_confirmation' if booking['payment_option']=='deposit' else 'paid_pending_confirmation', booking_id))
                con.execute("INSERT OR IGNORE INTO email_outbox(booking_id,kind) VALUES (?, 'paid')", (booking_id,))
                if booking['payment_option'] == 'deposit':
                    con.execute("UPDATE balance_payments SET status='scheduled' WHERE booking_id=? AND status='awaiting_deposit'", (booking_id,))
                should_send_email = True
        elif event_type == 'checkout.session.completed' and booking['status'] == 'awaiting_payment':
            con.execute("UPDATE bookings SET status='payment_processing' WHERE id=?", (booking_id,))
        elif event_type in ('checkout.session.async_payment_failed', 'checkout.session.expired') and booking['status'] in ('awaiting_payment', 'payment_processing'):
            con.execute("UPDATE bookings SET status='payment_failed' WHERE id=?", (booking_id,))
    if should_send_email:
        send_booking_email(booking_id, 'paid')
    return 'recorded'


@app.route('/balance/<token>', methods=['GET', 'POST'])
def balance_payment(token):
    try:
        booking_id = URLSafeSerializer(app.secret_key, salt='balance-payment-v1').loads(token)
    except BadSignature:
        abort(404)
    with db() as con:
        row = con.execute('''SELECT b.*,p.status AS balance_status,p.checkout_id,p.checkout_started_at
            FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == 'POST':
        csrf = session.get('csrf_token', '')
        if not csrf or not hmac.compare_digest(csrf, request.form.get('csrf_token', '')):
            abort(400)
        if row['balance_status'] != 'needs_manual':
            return redirect(url_for('balance_payment', token=token), code=303)
        if not row['checkout_id']:
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                con.execute('''UPDATE balance_payments SET checkout_started_at=?
                    WHERE booking_id=? AND status='needs_manual' AND checkout_started_at IS NULL''',
                    (datetime.now(timezone.utc).isoformat(), booking_id))
                row = con.execute('''SELECT b.*,p.status AS balance_status,p.checkout_id,p.checkout_started_at
                    FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
            if row['balance_status'] != 'needs_manual':
                return redirect(url_for('balance_payment', token=token), code=303)
        if not row['checkout_id'] and datetime.now(timezone.utc) - datetime.fromisoformat(row['checkout_started_at']) > timedelta(hours=20):
            with db() as con:
                con.execute("""UPDATE balance_payments SET status='needs_review',last_error='Recovery checkout creation outcome unresolved'
                    WHERE booking_id=? AND status='needs_manual' AND checkout_id IS NULL""", (booking_id,))
            return redirect(url_for('balance_payment', token=token), code=303)
        base = os.environ['PUBLIC_BASE_URL'].rstrip('/')
        try:
            client = stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])
            if row['checkout_id']:
                checkout = client.v1.checkout.sessions.retrieve(row['checkout_id'])
            else:
                checkout = client.v1.checkout.sessions.create({
                    'mode': 'payment', 'customer_email': row['email'], 'client_reference_id': booking_id,
                    'metadata': {'purpose': 'hire_balance'},
                    'line_items': [{'price_data': {'currency': 'gbp', 'unit_amount': row['subtotal_pence'] - row['paid_pence'],
                                                   'product_data': {'name': 'Jua Brothers outstanding hire balance'}}, 'quantity': 1}],
                    'success_url': base + url_for('balance_payment', token=token),
                    'cancel_url': base + url_for('balance_payment', token=token),
                }, options={'idempotency_key': 'balance-checkout-' + booking_id})
        except stripe.StripeError:
            app.logger.exception('Balance checkout creation failed for %s', booking_id)
            return render_template('balance.html', booking=row, error='Checkout unavailable. Please contact us.'), 503
        with db() as con:
            con.execute("UPDATE balance_payments SET checkout_id=? WHERE booking_id=? AND status='needs_manual' AND checkout_id IS NULL", (checkout.id, booking_id))
            current = con.execute('SELECT status,checkout_id FROM balance_payments WHERE booking_id=?', (booking_id,)).fetchone()
        if current['status'] != 'needs_manual' or current['checkout_id'] != checkout.id:
            return redirect(url_for('balance_payment', token=token), code=303)
        if checkout.status != 'open':
            if checkout.status == 'expired':
                with db() as con:
                    con.execute("""UPDATE balance_payments SET status='needs_review',last_error='Recovery checkout expired; contact Stripe before retry'
                        WHERE booking_id=? AND status='needs_manual' AND checkout_id=?""", (booking_id, checkout.id))
            return render_template('balance.html', booking={**dict(row), 'balance_status': 'processing'}, error=None)
        return redirect(checkout.url, code=303)
    response = app.make_response(render_template('balance.html', booking=row, error=None))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.route('/returns/<token>', methods=['GET', 'POST'])
def customer_return(token):
    if not app.secret_key:
        abort(503)
    try:
        booking_id = URLSafeSerializer(app.secret_key, salt='booking-return-v1').loads(token)
    except BadSignature:
        abort(404)
    with db() as con:
        booking = con.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
        if not booking or booking['status'] not in PAID_STATUSES:
            abort(404)
        if request.method == 'POST':
            csrf = session.get('csrf_token', '')
            if not csrf or not hmac.compare_digest(csrf, request.form.get('csrf_token', '')):
                abort(400, 'Invalid security token. Refresh the page and try again.')
            product_id = request.form.get('product_id', '')
            item = con.execute('''SELECT * FROM booking_items WHERE booking_id=? AND product_id=?''',
                               (booking_id, product_id)).fetchone()
            if not item:
                abort(404)
            if business_today() < date.fromisoformat(item['start_date']):
                flash('A return cannot be requested before the hire starts.', 'error')
            elif item['returned'] >= item['quantity']:
                flash('This product line is already fully returned.', 'success')
            elif item['return_requested']:
                flash('This return is already awaiting staff confirmation.', 'success')
            else:
                con.execute('UPDATE booking_items SET return_requested=1 WHERE booking_id=? AND product_id=?',
                            (booking_id, product_id))
                con.execute('''INSERT INTO return_events(booking_id,product_id,quantity,actor,created_at)
                    VALUES (?,?,?,?,?)''', (booking_id, product_id, item['quantity'] - item['returned'],
                                            'customer_request', datetime.now(timezone.utc).isoformat()))
                flash('Return requested. Staff must confirm receipt before stock is released.', 'success')
            return redirect(url_for('customer_return', token=token), code=303)
        items = con.execute('SELECT * FROM booking_items WHERE booking_id=?', (booking_id,)).fetchall()
    response = app.make_response(render_template('returns.html', booking=booking, items=items))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def admin_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return view(*args, **kwargs)
    return decorated


@app.before_request
def redirect_alternate_domains():
    hosts = {host.strip().lower() for host in os.getenv('REDIRECT_FROM_HOSTS', '').split(',') if host.strip()}
    if request.host.partition(':')[0].lower() not in hosts:
        return
    origin = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
    try:
        parsed = urlsplit(origin)
        valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and parsed.hostname.lower() not in hosts and (parsed.port is None or parsed.port > 0))
    except ValueError:
        valid = False
    if not valid:
        app.logger.error('PUBLIC_BASE_URL must be a distinct HTTPS origin for domain redirects')
        abort(503, 'Domain redirect is not configured correctly.')
    path = request.environ.get('RAW_URI', request.full_path)
    if not path.startswith('/'):
        path = request.full_path
    if path.endswith('?'):
        path = path[:-1]
    return redirect(origin + path, code=308)


@app.before_request
def protect_admin():
    if not request.path.startswith('/admin'):
        return
    if not app.secret_key or not os.getenv('ADMIN_USERNAME') or not os.getenv('ADMIN_PASSWORD'):
        abort(503, 'Admin credentials and SECRET_KEY must be configured.')
    if request.method == 'POST':
        token = session.get('csrf_token', '')
        if not token or not hmac.compare_digest(token, request.form.get('csrf_token', '')):
            abort(400, 'Invalid security token. Refresh the page and try again.')


@app.context_processor
def csrf_context():
    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']
    return {'csrf_token': csrf_token}


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=15)).isoformat()
        with db() as con:
            con.execute('DELETE FROM login_attempts WHERE created_at < ?', (cutoff,))
            attempts = con.execute(
                'SELECT COUNT(*) AS count FROM login_attempts WHERE ip=?', (ip,)
            ).fetchone()['count']
            if attempts >= 10:
                return render_template('admin_login.html', error='Too many attempts. Try again in 15 minutes.'), 429
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            valid = (hmac.compare_digest(username.encode(), os.environ['ADMIN_USERNAME'].encode())
                     and hmac.compare_digest(password.encode(), os.environ['ADMIN_PASSWORD'].encode()))
            if not valid:
                con.execute('INSERT INTO login_attempts(ip,created_at) VALUES (?,?)', (ip, now.isoformat()))
                return render_template('admin_login.html', error='Invalid username or password.'), 401
            con.execute('DELETE FROM login_attempts WHERE ip=?', (ip,))
        session.clear()
        session['admin'] = True
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_login.html', error=None)


@app.post('/admin/logout')
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.get('/admin')
@admin_required
def admin_dashboard():
    with db() as con:
        categories = con.execute('SELECT * FROM categories ORDER BY name').fetchall()
        products = catalog(con)
        pending_image_deletions = con.execute('SELECT COUNT(*) AS count FROM image_deletions').fetchone()['count']
        query = request.args.get('booking', '').strip()
        if query and not re.fullmatch(r'[a-fA-F0-9]{1,32}', query):
            flash('Enter a booking reference using hexadecimal characters.', 'error')
            query = ''
        bookings = [dict(row) for row in con.execute(
            'SELECT * FROM bookings WHERE id LIKE ? ORDER BY created_at DESC LIMIT 100',
            (query + '%',))]
        today = date.today()
        reservations = reservation_rows(con, today)
        stock = {p['id']: p['stock'] for p in products}
        conflicts = set()
        for row in reservations:
            if row['status'] not in PAID_STATUSES or date.fromisoformat(row['end_date']) < today or row['product_id'] not in stock:
                continue
            others = [other for other in reservations if other['id'] != row['id']]
            start = max(today, date.fromisoformat(row['start_date']))
            end = date.fromisoformat(row['end_date'])
            if row['quantity'] - row['returned'] + peak_reserved(others, row['product_id'], start, end, today) > stock[row['product_id']]:
                conflicts.add(row['id'])
        for product in products:
            product['available_today'] = max(0, product['stock'] - peak_reserved(reservations, product['id'], today, today, today))
        for booking in bookings:
            booking['items'] = con.execute('SELECT * FROM booking_items WHERE booking_id=?', (booking['id'],)).fetchall()
            booking['balance'] = con.execute('SELECT status,last_error,intent_id,checkout_id FROM balance_payments WHERE booking_id=?', (booking['id'],)).fetchone()
            booking['emails'] = con.execute('SELECT * FROM email_outbox WHERE booking_id=?', (booking['id'],)).fetchall()
            booking['returns'] = con.execute('SELECT * FROM return_events WHERE booking_id=? ORDER BY id DESC', (booking['id'],)).fetchall()
            booking['conflict'] = booking['id'] in conflicts
    return render_template('admin.html', categories=categories, products=products, bookings=bookings,
                           conflicts=sorted(conflicts), booking_query=query,
                           pending_image_deletions=pending_image_deletions)


@app.post('/admin/images/delete-retry')
@admin_required
def admin_retry_image_deletions():
    with db() as con:
        images = con.execute('SELECT image_url FROM image_deletions ORDER BY created_at').fetchall()
    failed = sum(not delete_queued_image(row['image_url']) for row in images)
    if failed:
        flash(f'{failed} product image(s) could not be deleted. Check storage and retry.', 'error')
    else:
        flash('Pending product image deletions completed.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/bookings/<booking_id>/returns/<product_id>')
@admin_required
def admin_confirm_return(booking_id, product_id):
    quantity = request.form.get('quantity', '')
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        item = con.execute('''SELECT i.*,b.status FROM booking_items i JOIN bookings b ON b.id=i.booking_id
            WHERE i.booking_id=? AND i.product_id=?''', (booking_id, product_id)).fetchone()
        if not item or item['status'] not in PAID_STATUSES:
            abort(404)
        remaining = item['quantity'] - item['returned']
        if not re.fullmatch(r'\d{1,5}', quantity) or not 1 <= int(quantity) <= remaining:
            flash(f'Enter a returned quantity between 1 and {remaining}.', 'error')
        else:
            amount = int(quantity)
            con.execute('''UPDATE booking_items SET returned=returned+?,
                return_requested=CASE WHEN returned+?=quantity THEN 0 ELSE return_requested END
                WHERE booking_id=? AND product_id=?''', (amount, amount, booking_id, product_id))
            con.execute('''INSERT INTO return_events(booking_id,product_id,quantity,actor,created_at)
                VALUES (?,?,?,?,?)''', (booking_id, product_id, amount, 'admin_confirmed',
                                        datetime.now(timezone.utc).isoformat()))
            flash(f'Confirmed {amount} returned. Stock availability has been updated.', 'success')
    return redirect(url_for('admin_dashboard') + '#bookings', code=303)


@app.post('/admin/bookings/<booking_id>/email/<kind>/retry')
@admin_required
def admin_retry_email(booking_id, kind):
    if kind not in ('paid', 'quote', 'balance_due'):
        abort(404)
    with db() as con:
        outbox = con.execute('SELECT status FROM email_outbox WHERE booking_id=? AND kind=?', (booking_id, kind)).fetchone()
    if not outbox:
        abort(404)
    send_booking_email(booking_id, kind)
    with db() as con:
        result = con.execute('SELECT status FROM email_outbox WHERE booking_id=? AND kind=?', (booking_id, kind)).fetchone()
    flash('Email sent.' if result['status'] == 'sent' else 'Email was not sent. Check SMTP configuration and logs.', 'success' if result['status'] == 'sent' else 'error')
    return redirect(url_for('admin_dashboard') + '#bookings', code=303)


@app.post('/admin/bookings/<booking_id>/payment/recheck')
@admin_required
def admin_recheck_payment(booking_id):
    with db() as con:
        booking = con.execute('SELECT stripe_session_id,payment_option FROM bookings WHERE id=?', (booking_id,)).fetchone()
    if not booking:
        abort(404)
    if not booking['stripe_session_id'] or booking['payment_option'] == 'quote':
        flash('No Stripe Checkout session is recorded for this booking.', 'error')
    else:
        try:
            checkout = stripe.StripeClient(os.environ['STRIPE_SECRET_KEY']).v1.checkout.sessions.retrieve(booking['stripe_session_id'])
            if checkout.get('client_reference_id') != booking_id:
                app.logger.error('Stripe checkout reference mismatch for booking %s', booking_id)
                result = 'unmatched'
            elif checkout.get('payment_status') != 'paid':
                result = 'unpaid'
            else:
                result = record_booking_checkout(checkout, 'checkout.session.completed')
        except (stripe.StripeError, KeyError):
            app.logger.exception('Stripe checkout recheck failed for booking %s', booking_id)
            result = 'error'
        if result == 'recorded':
            flash('Stripe confirmed the payment. Check the email status below.', 'success')
        else:
            flash('Payment was not confirmed. Check Stripe and try again; do not collect payment again without checking.', 'error')
    return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)


@app.post('/admin/bookings/<booking_id>/balance/recheck')
@admin_required
def admin_recheck_balance(booking_id):
    with db() as con:
        row = con.execute('''SELECT b.subtotal_pence,b.paid_pence,p.status,p.intent_id,p.checkout_id
            FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
    if not row:
        abort(404)
    if row['status'] != 'needs_review':
        flash('This balance is not awaiting review.', 'error')
        return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)
    if not row['checkout_id'] and not row['intent_id']:
        flash('No Stripe payment ID is recorded. Search Stripe by the full reference and contact support before accepting another payment.', 'error')
        return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)
    try:
        client = stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])
        if row['checkout_id']:
            payment = client.v1.checkout.sessions.retrieve(row['checkout_id'])
            valid = (payment.id == row['checkout_id'] and payment.client_reference_id == booking_id
                     and payment.metadata.get('purpose') == 'hire_balance' and payment.currency == 'gbp'
                     and payment.amount_total == row['subtotal_pence'] - row['paid_pence']
                     and payment.payment_status == 'paid')
        else:
            payment = client.v1.payment_intents.retrieve(row['intent_id'])
            valid = (payment.id == row['intent_id'] and payment.metadata.get('booking_id') == booking_id
                     and payment.metadata.get('purpose') == 'hire_balance' and payment.currency == 'gbp'
                     and payment.amount_received == row['subtotal_pence'] - row['paid_pence']
                     and payment.status == 'succeeded')
    except (stripe.StripeError, KeyError):
        app.logger.exception('Stripe balance recheck failed for %s', booking_id)
        flash('Could not check Stripe. Try again or contact support before taking payment.', 'error')
        return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)
    if valid:
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            current = con.execute('''SELECT b.subtotal_pence,b.paid_pence,p.status,p.intent_id,p.checkout_id
                FROM bookings b JOIN balance_payments p ON p.booking_id=b.id WHERE b.id=?''', (booking_id,)).fetchone()
            if (current and current['status'] == 'needs_review'
                    and current['subtotal_pence'] == row['subtotal_pence'] and current['paid_pence'] == row['paid_pence']
                    and current['checkout_id'] == row['checkout_id'] and current['intent_id'] == row['intent_id']):
                con.execute('UPDATE bookings SET paid_pence=subtotal_pence WHERE id=?', (booking_id,))
                con.execute("UPDATE balance_payments SET status='paid',last_error=NULL WHERE booking_id=?", (booking_id,))
                flash('Stripe confirmed the balance was paid.', 'success')
            else:
                flash('The balance changed during the check. Refresh and review it.', 'error')
    else:
        flash('Stripe has not confirmed a matching successful payment. Review in Stripe before taking another payment.', 'error')
    return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)


@app.post('/admin/bookings/<booking_id>/balance/stop')
@admin_required
def admin_stop_balance(booking_id):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT status FROM balance_payments WHERE booking_id=?', (booking_id,)).fetchone()
        if not row:
            abort(404)
        stopped = con.execute("""UPDATE balance_payments SET status='stopped',
            last_error='Stopped by admin; refund and booking require manual handling'
            WHERE booking_id=? AND status='scheduled'""", (booking_id,))
        if stopped.rowcount:
            flash('Scheduled balance stopped. Handle the refund, booking and stock separately.', 'success')
        else:
            flash('The balance could not be stopped: it may already be processing or paid. Check Stripe before refunding.', 'error')
    return redirect(url_for('admin_dashboard', booking=booking_id) + '#bookings', code=303)


@app.post('/admin/categories')
@admin_required
def admin_category_save():
    name = request.form.get('name', '').strip()
    category_id = request.form.get('id', '')
    if not name or len(name) > 100:
        flash('Category names must contain 1 to 100 characters.', 'error')
    else:
        try:
            with db() as con:
                if category_id:
                    result = con.execute('UPDATE categories SET name=? WHERE id=?', (name, category_id))
                    if not result.rowcount:
                        abort(404)
                else:
                    con.execute('INSERT INTO categories(id,name) VALUES (?,?)', (uuid.uuid4().hex, name))
            flash('Category saved.', 'success')
        except (sqlite3.IntegrityError, psycopg.IntegrityError):
            flash('That category name already exists.', 'error')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/categories/<category_id>/delete')
@admin_required
def admin_category_delete(category_id):
    with db() as con:
        if con.execute('SELECT 1 FROM products WHERE category_id=? AND active=1', (category_id,)).fetchone():
            flash('Move or remove the products in this category before deleting it.', 'error')
        else:
            con.execute('DELETE FROM products WHERE category_id=? AND active=0', (category_id,))
            if not con.execute('DELETE FROM categories WHERE id=?', (category_id,)).rowcount:
                abort(404)
            flash('Category deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/products/new', methods=['GET', 'POST'])
@app.route('/admin/products/<product_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(product_id=None):
    with db() as con:
        product = con.execute('SELECT * FROM products WHERE id=? AND active=1', (product_id,)).fetchone() if product_id else None
        categories = con.execute('SELECT * FROM categories ORDER BY name').fetchall()
    if product_id and product is None:
        abort(404)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        category_id = request.form.get('category_id', '')
        price = request.form.get('price', '').strip()
        stock = request.form.get('stock', '').strip()
        try:
            if not re.fullmatch(r'\d{1,6}(?:\.\d{1,2})?', price):
                raise ValueError
            pence = int(Decimal(price) * 100)
            if not 1 <= pence <= 99999999 or not re.fullmatch(r'\d{1,5}', stock) or not 1 <= int(stock) <= 10000:
                raise ValueError
        except (ValueError, InvalidOperation):
            flash('Enter a price greater than zero and stock between 1 and 10,000.', 'error')
            return render_template('admin_product.html', product=product, categories=categories), 400
        if not name or len(name) > 120 or len(description) > 2000 or not description or category_id not in {c['id'] for c in categories}:
            flash('Enter a name, description and valid category.', 'error')
            return render_template('admin_product.html', product=product, categories=categories), 400
        upload = request.files.get('image')
        filename = None
        if upload and upload.filename:
            try:
                image = Image.open(upload.stream)
                if image.width * image.height > 20_000_000:
                    raise ValueError('Image dimensions are too large')
                image.verify()
                extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}.get(image.format)
                if not extension:
                    raise ValueError('Unsupported image format')
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                flash('Choose a valid JPEG, PNG or WebP image.', 'error')
                return render_template('admin_product.html', product=product, categories=categories), 400
            filename = uuid.uuid4().hex + '.' + extension
            content_type = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}[extension]
        old_image_url = None
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            if product:
                existing = con.execute('SELECT stock,image_url FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
                if not existing:
                    abort(404)
                if int(stock) < existing['stock']:
                    today = date.today()
                    reserved = peak_reserved(reservation_rows(con, today), product_id, today, date.max, today)
                    if int(stock) < reserved:
                        flash(f'Total stock cannot be below {reserved}, the maximum already reserved.', 'error')
                        return render_template('admin_product.html', product=product, categories=categories), 400
                old_image_url = existing['image_url'] if filename else None
                image_url = url_for('uploaded_image', filename=filename) if filename else existing['image_url']
                if filename:
                    store_product_image(upload, filename, content_type)
                con.execute('''UPDATE products SET name=?,description=?,category_id=?,price_pence=?,
                    stock=?,image_url=? WHERE id=? AND active=1''',
                    (name, description, category_id, pence, int(stock), image_url, product_id))
                queue_image_deletion(con, old_image_url)
            else:
                image_url = url_for('uploaded_image', filename=filename) if filename else ''
                if filename:
                    store_product_image(upload, filename, content_type)
                con.execute('''INSERT INTO products(id,name,description,category_id,price_pence,stock,image_url)
                    VALUES (?,?,?,?,?,?,?)''', (uuid.uuid4().hex, name, description, category_id, pence, int(stock), image_url))
        flash('Product saved.', 'success')
        if managed_image_filename(old_image_url) and not delete_queued_image(old_image_url):
            flash('The old product image could not be deleted. Retry image deletion in admin.', 'error')
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_product.html', product=product, categories=categories)


@app.post('/admin/products/<product_id>/delete')
@admin_required
def admin_product_delete(product_id):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        product = con.execute('SELECT image_url FROM products WHERE id=? AND active=1', (product_id,)).fetchone()
        if not product:
            abort(404)
        con.execute("UPDATE products SET active=0,image_url='' WHERE id=? AND active=1", (product_id,))
        queue_image_deletion(con, product['image_url'])
    flash('Product removed from the storefront.', 'success')
    if managed_image_filename(product['image_url']) and not delete_queued_image(product['image_url']):
        flash('The product image could not be deleted. Retry image deletion in admin.', 'error')
    return redirect(url_for('admin_dashboard'))


@app.get('/uploads/<filename>')
def uploaded_image(filename):
    if not re.fullmatch(r'[0-9a-f]{32}\.(?:jpg|png|webp)', filename):
        abort(404)
    container = get_image_container()
    if container:
        try:
            blob = container.get_blob_client(filename).download_blob()
        except ResourceNotFoundError:
            abort(404)
        content_type = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp'}[filename.rsplit('.', 1)[1]]
        return send_file(io.BytesIO(blob.readall()), mimetype=content_type, max_age=31536000)
    return send_from_directory(UPLOAD_PATH, filename)


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG') == '1')
