# Jua Brothers — Flask + Stripe starter

This package keeps the supplied HTML homepage and adds a booking form, a 30% deposit option, full payment, and quote requests. Prices are taken from the supplied HTML, in GBP. There are **no delivery charges**. The existing logo reference expects `static/logo.png`; add your actual logo there.

## Run locally

1. Install Python 3.10+.
2. In this folder, run `python -m venv .venv` and activate it (`source .venv/bin/activate` on macOS/Linux; `.venv\\Scripts\\activate` on Windows).
3. Run `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env` and set your **Stripe test secret key**. Never put this key in HTML or commit `.env` to Git.
5. Run `python app.py` and visit http://127.0.0.1:5000.
6. For local webhook testing, install Stripe CLI, sign in, and run `stripe listen --forward-to localhost:5000/stripe/webhook`. Put the displayed `whsec_...` signing secret in `.env` and restart Flask.
7. Use Stripe's official test card numbers in **test mode**. Confirm that the booking status changes only after a verified webhook.

## Important production tasks

- **Not production ready as-is.** Stock is set to 100 units of each of the six product types. The backend checks overlapping hire dates atomically for paid bookings and pending checkouts (pending checkouts hold stock for 30 minutes). Quote requests do not reserve inventory. Add admin review/confirmation, cancellation/refund flows, and a policy for disputes and payment races before launch. Paid requests remain provisional pending staff approval.
- Add a secure admin dashboard or staff notifications for quote requests and new bookings; currently they are stored only in SQLite. A quote request does **not** automatically email staff or the customer.
- Implement the **remaining 70% balance collection** (e.g. a second Stripe Checkout Session) and reconciliation. The current code records the outstanding balance but does not charge it.
- Clarify whether the listed prices are per day, per event, or for a defined hire duration. The current calculation is quantity × listed price, irrespective of dates.
- Configure VAT/tax treatment, deposit/refund policies, hire terms, privacy notice, and data retention before launch.
- Host behind HTTPS with a production WSGI server, configure Stripe live keys and a public webhook endpoint, persistent storage/backups, monitoring, and operational access controls. Do not use Flask's development server in production.
- Protect against abuse (rate limiting, spam prevention, CSRF protection where appropriate). Avoid exposing customer personal data in status pages.
- Review Stripe's payment method eligibility and any additional verification requirements for your UK business.

## Project files

- `app.py` — Flask routes, server-side pricing, Stripe checkout and verified webhook
- `templates/index.html` — original website with booking section
- `templates/status.html` — quote/payment status
- `static/` — put your existing `logo.png` here

## Notes

- The checkout amount is always computed on the server from fixed catalog prices, never trusted from HTML.
- Successful return to the website is **not** proof of payment. Only verified Stripe webhooks mark payment received.
- For an actual booking confirmation, stock availability and operational approval must be implemented.

## Inventory rules

Each of the six products has 100 pieces. Overlapping bookings share the same stock pool; bookings on non-overlapping dates do not reduce availability. Checkout attempts reserve stock for 30 minutes. Quote requests do not reserve stock. This is a starter: production deployment should also reconcile late payment events, cancellations/refunds, and any changes to reservations.


## Right-side shopping cart update
Each product card has a quantity selector and Add to Cart button. The Cart button in the navigation opens a right-side drawer. Customers can adjust/remove products, choose full payment, 30% deposit, or quote, and submit one combined booking. Cart state is kept in browser sessionStorage for the current tab.

Run on Mac: `python3 -m pip install -r requirements.txt` then `python3 app.py`; open http://127.0.0.1:5000. Stripe test credentials and webhook setup are needed for payments. Stock limits are 100 per type and overlapping bookings are checked server-side. Quote requests do not reserve stock. This is a development starter: test thoroughly and configure tax, policies, balance collection, email notifications, and production deployment before going live.
