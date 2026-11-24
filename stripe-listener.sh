#!/bin/sh
set -eu

case "${STRIPE_API_KEY:-}" in
    sk_test_*|rk_test_*) ;;
    *) echo "Stripe listener requires a test-mode secret or restricted key." >&2; exit 1 ;;
esac
if [ -z "${STRIPE_WEBHOOK_SECRET:-}" ]; then
    echo "Stripe listener requires STRIPE_WEBHOOK_SECRET." >&2
    exit 1
fi

if ! actual_secret=$(/bin/stripe listen --print-secret); then
    echo "Could not retrieve the Stripe CLI signing secret." >&2
    exit 1
fi
if [ "$actual_secret" != "$STRIPE_WEBHOOK_SECRET" ]; then
    echo "Stripe CLI signing secret does not match STRIPE_WEBHOOK_SECRET; see README setup." >&2
    exit 1
fi

/bin/stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired \
    --forward-to http://app:5000/stripe/webhook 2>&1 |
    awk '{gsub(/whsec_[A-Za-z0-9_]+/, "[redacted]"); print; fflush()}'
