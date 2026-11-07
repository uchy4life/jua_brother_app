"""Jua Brothers demo booking backend. Configure Stripe and business rules before launch."""
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import stripe
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, url_for

load_dotenv()
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024
DB_PATH = Path(os.getenv('DATABASE_PATH', str(Path(__file__).parent / 'bookings.db')))
DEPOSIT_PERCENT = 30
CATALOG = [
    {'id':'chiavari','name':'Chiavari Chairs','price_pence':350,'stock':100},
    {'id':'banquet_chairs','name':'Banquet Chairs','price_pence':400,'stock':100},
    {'id':'folding_chairs','name':'Folding Chairs','price_pence':250,'stock':100},
    {'id':'banquet_tables','name':'Banquet Tables','price_pence':500,'stock':100},
    {'id':'round_tables','name':'Round Tables','price_pence':800,'stock':100},
    {'id':'folding_tables','name':'Folding Tables','price_pence':700,'stock':100},
]


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT NOT NULL,
            email TEXT NOT NULL, phone TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            items_json TEXT NOT NULL, subtotal_pence INTEGER NOT NULL,
            payment_option TEXT NOT NULL, due_pence INTEGER NOT NULL,
            paid_pence INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            stripe_session_id TEXT UNIQUE
        )''')
        con.execute('''CREATE TABLE IF NOT EXISTS stripe_events (id TEXT PRIMARY KEY)''')

init_db()


@app.get('/')
def home():
    return render_template('index.html', catalog=CATALOG, today=date.today().isoformat(), error=None)


def invalid(message):
    return render_template('index.html', catalog=CATALOG, today=date.today().isoformat(), error=message), 400


@app.post('/book')
def create_booking():
    name = request.form.get('customer_name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    option = request.form.get('payment_option', '')
    if not name or len(name) > 120 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email)>254 or len(phone)>40:
        return invalid('Please enter a valid name, email and phone number.')
    if option not in ('full', 'deposit', 'quote'):
        return invalid('Please select a payment option.')
    try:
        start = date.fromisoformat(request.form['start_date'])
        end = date.fromisoformat(request.form['end_date'])
    except (KeyError, ValueError):
        return invalid('Please enter valid hire dates.')
    if start < date.today() or end < start:
        return invalid('The hire dates must be in the future and the end cannot precede the start.')
    items = []
    for product in CATALOG:
        raw = request.form.get('qty_' + product['id'], '0')
        if not re.fullmatch(r'\d{1,5}', raw):
            return invalid('Please enter whole-number quantities between 0 and 10,000.')
        quantity = int(raw)
        if quantity > product['stock']:
            return invalid(f"Only {product['stock']} units of {product['name']} are owned. Please reduce your quantity.")
        if quantity:
            items.append({**product, 'quantity': quantity})
    if not items:
        return invalid('Select at least one chair or table.')
    subtotal = sum(p['price_pence'] * p['quantity'] for p in items)
    # Rounded to nearest penny, with .5 rounded upward for positive integers.
    due = 0 if option == 'quote' else (subtotal * DEPOSIT_PERCENT + 50) // 100 if option == 'deposit' else subtotal
    booking_id = uuid.uuid4().hex
    status = 'quote_requested' if option == 'quote' else 'awaiting_payment'
    # Reserve availability atomically for payment attempts and paid bookings.
    # Quotes are requests only and do not reserve stock.
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        if option != 'quote':
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            overlaps = con.execute('''SELECT items_json FROM bookings
                WHERE start_date <= ? AND end_date >= ? AND (
                  status IN ('paid_pending_confirmation', 'deposit_paid_pending_confirmation')
                  OR (status='awaiting_payment' AND created_at >= ?))''',
                (end.isoformat(), start.isoformat(), cutoff)).fetchall()
            reserved = {p['id']: 0 for p in CATALOG}
            for existing in overlaps:
                for entry in json.loads(existing['items_json']):
                    reserved[entry['id']] += entry['quantity']
            for item in items:
                remaining = item['stock'] - reserved[item['id']]
                if item['quantity'] > remaining:
                    return invalid(f"Only {remaining} {item['name']} available for those dates. Choose fewer or request a quote.")
        con.execute('''INSERT INTO bookings
            (id,created_at,name,email,phone,start_date,end_date,items_json,subtotal_pence,payment_option,due_pence,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (booking_id,datetime.now(timezone.utc).isoformat(),name,email,phone,start.isoformat(),end.isoformat(),json.dumps(items),subtotal,option,due,status))
    if option == 'quote':
        return redirect(url_for('booking_status', booking_id=booking_id))
    if not os.getenv('STRIPE_SECRET_KEY'):
        return invalid('Online checkout is not configured yet. Please contact us for a quote.')
    stripe.api_key = os.environ['STRIPE_SECRET_KEY']
    try:
        session = stripe.checkout.Session.create(
            mode='payment',
            customer_email=email,
            client_reference_id=booking_id,
            line_items=[{'price_data': {'currency':'gbp', 'unit_amount':due,
                         'product_data': {'name': 'Jua Brothers hire — ' + ('30% deposit' if option == 'deposit' else 'full payment')}},
                         'quantity':1}],
            metadata={'booking_id':booking_id},
            expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
            success_url=url_for('booking_status', booking_id=booking_id, _external=True) + '?checkout=returned',
            cancel_url=url_for('booking_status', booking_id=booking_id, _external=True) + '?checkout=cancelled',
        )
    except stripe.StripeError:
        app.logger.exception('Stripe checkout session creation failed')
        return invalid('Payment checkout is temporarily unavailable. Please try again later.')
    with db() as con:
        con.execute('UPDATE bookings SET stripe_session_id=? WHERE id=?', (session.id, booking_id))
    return redirect(session.url, code=303)


@app.get('/booking/<booking_id>')
def booking_status(booking_id):
    with db() as con:
        row = con.execute('SELECT id, payment_option, status, subtotal_pence, due_pence, paid_pence FROM bookings WHERE id=?', (booking_id,)).fetchone()
    if row is None:
        abort(404)
    # Booking IDs are unguessable, but do not show private contact data on this public page.
    return render_template('status.html', booking=row)


@app.post('/stripe/webhook')
def stripe_webhook():
    secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    if not secret:
        abort(503)
    try:
        event = stripe.Webhook.construct_event(request.get_data(), request.headers.get('Stripe-Signature', ''), secret)
    except (ValueError, stripe.SignatureVerificationError):
        abort(400)
    if event['type'] not in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        return '', 200
    session = event['data']['object']
    if session.get('payment_status') != 'paid' or session.get('currency') != 'gbp':
        return '', 200
    booking_id = session.get('client_reference_id')
    with db() as con:
        booking = con.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
        if not booking or booking['stripe_session_id'] != session['id'] or booking['due_pence'] != session.get('amount_total'):
            app.logger.error('Unmatched Stripe payment session %s', session.get('id'))
            abort(400)
        if con.execute('SELECT 1 FROM stripe_events WHERE id=?', (event['id'],)).fetchone():
            return '', 200
        con.execute('INSERT INTO stripe_events(id) VALUES (?)', (event['id'],))
        if booking['status'] == 'awaiting_payment':
            con.execute('UPDATE bookings SET paid_pence=?, status=? WHERE id=?',
                        (booking['due_pence'], 'deposit_paid_pending_confirmation' if booking['payment_option']=='deposit' else 'paid_pending_confirmation', booking_id))
    return '', 200


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG') == '1')
