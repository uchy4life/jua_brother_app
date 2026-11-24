# Jua Brothers rental app

The storefront, admin, SQLite database and Stripe Checkout run in one Flask service. Products and categories are stored in the database. Prices are in GBP **per item per calendar day**, including both hire dates.

The cart count is the number of different products, not total units. Each product card shows availability today, regardless of selected hire dates. Counts refresh every 30 seconds while the page is visible and when it regains focus, but are snapshots rather than reservations. Customers can enter a requested quantity on each product card; adding it sets that product's cart quantity. Each cart line checks date-specific availability, and paid checkout rechecks it when submitted. Quote requests do not reserve stock. The experimental deposit workflow uses 00:00 UK time on the earliest selected product's start date for customers consenting to the current terms; older recorded consents retain their 9:00 AM timing. Same-day hires require full payment. Existing bookings keep their original hire dates.

Checkout and quote submissions require acceptance of the displayed terms. The terms version and acceptance time are saved with the booking. Update `TERMS_VERSION` whenever the terms change and retain old versions for audit; if a new version includes midnight balance consent, add it to the worker's `MIDNIGHT_CONSENT_TERMS_VERSIONS`. The terms are a draft and require UK legal review before publication. Staff handle cancellations and refunds manually; the application does not process them. Delivery charges are agreed separately and are not included in the online subtotal.

Before manually refunding a deposit for a cancellation, staff must use **Stop scheduled balance** on that booking in admin and verify it shows `stopped`. This does not issue a refund, cancel the booking or release stock. If the balance is already processing or paid, the stop action refuses and staff must check Stripe before refunding. The worker also flags an already refunded or disputed deposit for manual review rather than initiating a balance charge. This check is not a substitute for stopping the balance: it cannot prevent a refund started concurrently with collection or reliably detect a pending refund.

The submitted cart is cleared in the same browser tab when a quote is recorded or the customer returns from a completed Stripe Checkout. A cancelled checkout, failed payment or validation error keeps the cart. Cart edits made after submission are preserved. Returning from Checkout does not confirm payment; the verified webhook remains the source of payment status.

## Start with Docker Compose

1. Ensure docker and docker compose are installed
2. cd /jua_brother_app, copy `.env.example` to `.env`. Set a long, random `SECRET_KEY`, a unique `ADMIN_USERNAME` and a strong `ADMIN_PASSWORD`. For example, generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` as described below.
4. Run `docker compose up --build -d`, then open `http://localhost:5000`. Sign in at `http://localhost:5000/admin`.
5. Use `docker compose logs -f app` for logs and `docker compose down` to stop. `docker compose up -d` starts it again. The local test-mode Stripe listener runs as a Compose service.

The named `rental_data` volume holds both the SQLite database and uploaded images across restarts and rebuilds. The catalog migration runs when the existing database is first opened.

## Azure App Service

The production Docker deployment runs the Flask web container. App Service terminates HTTPS; Stripe sends its public webhook directly to `/stripe/webhook`. Do not deploy the local `stripe-listener` service. The experimental worker now has a PostgreSQL advisory lock, but keep `BALANCE_COLLECTION_ENABLED` disabled until the full payment and recovery workflow has been verified in an isolated Stripe sandbox and production monitoring is in place.

The app keeps SQLite and local uploads for Docker Compose development. In App Service it requires PostgreSQL and Blob Storage rather than silently falling back to ephemeral container storage. Configure these application settings:

- `DATABASE_URL`: Azure Database for PostgreSQL Flexible Server connection URI with TLS (`sslmode=require`).
- `AZURE_STORAGE_ACCOUNT_URL`: the Blob service URL, such as `https://<account>.blob.core.windows.net`.
- `AZURE_STORAGE_CONTAINER`: private container name; defaults to `product-images`.
- `WEBSITES_PORT`: `5000`.
- `PUBLIC_BASE_URL`: the public HTTPS origin; use the App Service hostname until custom-domain DNS is approved.
- `REDIRECT_FROM_HOSTS`: optional comma-separated alternate hostnames to redirect to `PUBLIC_BASE_URL` with HTTP 308, preserving the path and query. The target must be a distinct HTTPS origin. Configure HTTPS certificates for every alternate hostname.
- `SESSION_COOKIE_SECURE`: `1`.
- `BALANCE_COLLECTION_ENABLED`: `0`.
- `BALANCE_WORKER_IN_WEB`: `0` until the worker has been verified and enabled. When set to `1`, the container supervises the balance worker alongside the web server; do not also set it in the local Compose services, which already run a separate worker.

Enable the App Service system-assigned managed identity and grant it **Storage Blob Data Contributor** on the storage account. Keep the image container private; the app serves images through `/uploads/<filename>`. Store `DATABASE_URL`, `SECRET_KEY`, the admin credentials, Stripe keys and EmailJS private key in Key Vault and expose them to App Service using Key Vault references. Do not configure a storage account key or put secrets in the image.

Use the existing `Dockerfile` to build an image in Azure Container Registry and configure App Service for that image. Set the health-check path to `/healthz`. Start with a single App Service instance and keep the experimental worker disabled; verify PostgreSQL concurrency and recovery before scaling out. The PostgreSQL schema is initialized by the app. A new PostgreSQL database intentionally does not seed `initial_catalog.json`; import the live catalogue before opening the site to customers.

### Catalogue-only migration

The import script reads categories, products, stock counts and product image files only. It never queries bookings, so test bookings are not copied. Ensure the source SQLite file and its corresponding uploads folder are available, configure `DATABASE_URL` and `AZURE_STORAGE_ACCOUNT_URL`, and authenticate with an identity that can write to both targets:

To export the local Compose catalogue without copying bookings, start the `app` service and run the following from the app directory. The export directory must not already exist in the volume or on the host:

```powershell
Get-Content -Raw .\deploy\import_catalog.py | docker compose exec -T app python - --source-db /data/bookings.db --source-uploads /data/uploads --export-to /data/catalog-export
docker compose cp app:/data/catalog-export .\catalog-export
```

The export contains a catalogue-only SQLite database and referenced uploaded images. Review the counts and protect the export as business data. To migrate it, use `--source-db .\catalog-export\catalog.db --source-uploads .\catalog-export\uploads` in the commands below.

```powershell
python .\deploy\import_catalog.py --source-db .\bookings.db --source-uploads .\uploads
python .\deploy\import_catalog.py --source-db .\bookings.db --source-uploads .\uploads --apply
```

The first command is a dry run. The apply command refuses a non-empty PostgreSQL catalogue. It can be rerun if an upload or database transaction fails. Do not use the test database as the source for production.

## Admin

The shared account is configured in `.env`. Restart the service after changing credentials; rotate `SECRET_KEY` too to invalidate existing sessions and customer return links. The admin can create, rename and delete categories; create, edit and remove products; set daily prices and the total number owned; and upload JPEG, PNG or WebP images up to 6 MB. Existing bookings keep their item and price snapshots when products are edited or removed. Removed catalog products are hidden, not restored automatically. Categories containing active products must be emptied before deletion. Replaced images and images of removed products are deleted from app-managed storage immediately unless another active product uses them; external images are not deleted. If deletion fails, admin shows a warning and a retry button. Images orphaned before this feature was added are not automatically removed. Azure soft delete, versioning, backups or external caches may retain copies independently of the app.

## Booking emails and returns

Configure `EMAILJS_SERVICE_ID`, `EMAILJS_TEMPLATE_ID`, `EMAILJS_PUBLIC_KEY` and `EMAILJS_PRIVATE_KEY` in the private `.env` file. In EmailJS, configure the selected template's recipient as `{{to_email}}`, subject as `{{email_subject}}`, and body as `{{email_message}}`; set and verify the sender in the EmailJS service/template settings. The backend calls EmailJS's REST API, keeps the private key server-side, and spaces sends to respect its one-request-per-second limit. Never put credentials in the example file or source control. Compose `.env` values containing `$` should be single-quoted. Set `PUBLIC_BASE_URL` to the public HTTPS address so links work for customers.

After Stripe verifies a payment, customers receive booking details and a private return link. Quote requests receive an acknowledgement without a return link; they do not reserve stock. After physically returning a product line, the customer can request confirmation. A request does not release stock. The admin can confirm the whole line or enter a smaller quantity, including without a customer request. The remaining quantity stays pending. Return events and timestamps appear under each booking in the admin.

If a paid Checkout remains unverified because its webhook was missed, find the booking in admin and use **Check payment in Stripe**. This retrieves the recorded session from Stripe, verifies the reference, amount and currency, and records the payment and queues the booking email only when Stripe reports it paid. Check the email status and use its retry button if delivery failed. This action does not initiate a payment. If the item is physically returned, the per-item **Confirm return** controls appear after the payment is recorded.

Paid bookings and unexpired Checkout attempts reserve quantities for their dates. Once the scheduled end date has passed, any unconfirmed quantity remains unavailable for new hires until an admin confirms its return. Future reservations made before an item became overdue are not cancelled: the admin dashboard flags conflicting bookings for manual resolution. Stock counts and conflicts reflect the current server date. The dashboard shows the latest 100 matching bookings; search by reference to find older bookings. Availability today is shown separately from total stock. Historical paid bookings are migrated as unreturned because the old system did not track returns; review and confirm any already returned items. Historical quote and paid emails are queued for **manual** retry, not sent automatically.

Email send failures are logged and shown beside the booking with a retry button. Booking and quote emails require manual retry; the experimental balance worker retries failed balance emails after 15 minutes. Email sending is attempted after the booking or verified payment is saved, so a failed email does not undo a booking or payment. A crash during sending can result in a duplicate when retried; SMTP delivery itself cannot guarantee exactly-once delivery. Treat return links as private; anyone with a link can request (but cannot confirm) a return. Confirm physical receipt before releasing stock.

## Stripe

- Add a test-mode restricted API key with permission to create Checkout Sessions to `STRIPE_SECRET_KEY` in the private `.env`; a test secret key also works. The publishable key alone cannot create sessions, and hosted Checkout does not require it in the frontend. Hosted Checkout handles payment details; no card information is stored by this app. Use a secret manager in production.
- Configure a Stripe webhook for `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, and `checkout.session.expired` at `https://YOUR-DOMAIN/stripe/webhook` and set its signing secret in `STRIPE_WEBHOOK_SECRET`. Paid checkout is disabled until both the API key and signing secret are configured.
- For local testing, Compose runs `stripe-listener` using the app's test-mode Stripe key and forwards events directly to `app:5000`. It refuses live keys and checks that its signing secret matches `STRIPE_WEBHOOK_SECRET`; it does not print that secret. Check `docker compose logs stripe-listener` and `docker compose ps` for startup errors. Do not run a separate CLI listener at the same time: duplicate deliveries can occur. The Stripe CLI requires internet access; events missed while Docker or the computer is off are not guaranteed to be replayed. Recheck affected payments in admin.
- To configure the local signing secret, first put a test secret or restricted key in `.env`, then run `docker compose run --rm --no-deps --entrypoint /bin/stripe stripe-listener listen --print-secret`. Copy the displayed secret privately to `STRIPE_WEBHOOK_SECRET` in `.env`, then recreate the app and listener with `docker compose up -d`. Treat the printed value as a secret. The CLI's signing secret is separate from the signing secret for a Stripe Dashboard webhook endpoint. A restricted key must also have the permissions needed by the CLI listener.
- The Compose listener is for local **test mode only**, not a production webhook endpoint. In production configure the public HTTPS Stripe webhook described above and omit the local listener service. A local `PUBLIC_BASE_URL` means customer email links work only on this computer.
- Set `PUBLIC_BASE_URL` to the actual public HTTPS origin in production; Stripe uses it for return links. Set `SESSION_COOKIE_SECURE=1` behind HTTPS. Do not expose the admin on plain HTTP in production.
- A return from Checkout is not proof of payment: only a verified webhook records the initial payment. Full payment and quote requests remain available; **new deposit bookings and automatic balance collection are disabled by default** while the experimental balance workflow awaits end-to-end verification. Same-day hires require full payment, using the Europe/London calendar date. Quote requests take no payment and reserve no inventory.
- Delayed payments remain processing and hold inventory until Stripe reports success or failure. An event received after the initial hold has lapsed may conflict with an already accepted booking; payment is recorded and the admin must resolve the flagged conflict manually. Always confirm availability before promising fulfillment.

### Experimental deposit balance collection

Keep `BALANCE_COLLECTION_ENABLED=0` until the complete workflow has been tested with your Stripe test account and reviewed for your business. Do not rely on it for real deposits yet. When enabled in both services, deposit checkout requests explicit permission to save a card; the worker attempts the remaining balance at or after 00:00 Europe/London on the hire start date for new midnight-consent bookings. Older consent retains the previous 9:00 AM time. If the worker was offline, it catches up on restart. Failed card payments are canceled before a private Stripe Checkout payment link is emailed. Existing deposits without recorded consent are never automatically charged and receive a payment link when due.

The `balance-worker` service must stay running with the same persistent database volume as the app. A separate SQLite lock file in that volume serializes worker cycles; do not remove it or run workers against copies of the database. PostgreSQL uses a session advisory lock to serialize worker cycles. For the single-container deployment, `BALANCE_WORKER_IN_WEB=1` supervises the worker with the web process; App Service must also have Always On enabled. The `/healthz/balance-worker` endpoint reports a failed status if collection is disabled or no worker cycle has completed in ten minutes, and new deposit checkouts are refused until a recent worker heartbeat exists. Configure an external availability alert before activation. A heartbeat does not mean every payment or email succeeded, so also monitor worker logs and the admin balance and email statuses. Ambiguous Stripe outcomes and expired or failed recovery sessions are marked `needs_review` and require staff to check Stripe before arranging payment; the app does not automatically create a replacement link. The admin's Stripe recheck can record a matching successful payment when a Stripe ID is known, but cannot resolve unknown outcomes or create replacement links. A submitted recovery payment may remain processing until a verified Checkout webhook arrives. Recovery links are private; anyone with the link can open the payment page. A successful automatic balance payment is recorded by worker reconciliation, not by a PaymentIntent webhook.

Use test-mode keys and test cards and verify authorization, on-time and catch-up charges, declines, authentication-required payments, recovery links, webhook delivery, email, restarts and reconciliation before enabling. Stripe credentials need permissions for Checkout Sessions, Customers and PaymentIntents. Refunds, cancellations, payment confirmation emails for balances, automated alerting and a complete manual reconciliation workflow are not implemented. Review Stripe status before taking any manual payment to avoid charging twice.

## Run and test without Docker

Install Python 3.10+ and dependencies with `python -m pip install -r requirements.txt`, create `.env` as above, and run `python app.py`. To run regression tests: `python -m unittest discover -s tests -v`. The development server is not intended for production.

## Before production

This is a starter, not a fully operational rental system. Paid requests remain provisional. Add booking confirmation, cancellation, refunds, complete remaining-balance reconciliation and monitoring. Pending Checkout attempts initially hold stock for 35 minutes; late or delayed webhook delivery can lead to reservations that need manual reconciliation. Configure tax, rental terms, privacy, retention and dispute policies. Deploy behind HTTPS with appropriate authentication, rate limiting, backup/restore procedures and access controls. The included login throttling is per observed client IP; configure a trusted proxy and appropriate edge rate limiting for a public deployment. The SQLite service is intended for a single instance, not multiple replicas sharing a volume. The original logo is not included; place your logo at `static/logo.png`.
