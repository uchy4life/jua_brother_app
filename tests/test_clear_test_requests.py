import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    'clear_test_requests', Path(__file__).resolve().parents[1] / 'deploy' / 'clear_test_requests.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupValidationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'id': 'a', 'payment_option': 'full', 'status': 'payment_failed',
             'paid_pence': 0, 'stripe_session_id': 'cs_a'},
            {'id': 'b', 'payment_option': 'full', 'status': 'payment_failed',
             'paid_pence': 0, 'stripe_session_id': 'cs_b'},
            {'id': 'c', 'payment_option': 'quote', 'status': 'quote_requested',
             'paid_pence': 0, 'stripe_session_id': None},
        ]

    def test_requires_exact_unpaid_expected_records(self):
        cleanup.validate_bookings(self.rows)
        for field, value in [('status', 'payment_processing'), ('paid_pence', 1),
                             ('stripe_session_id', None)]:
            with self.subTest(field=field):
                changed = [dict(row) for row in self.rows]
                changed[0][field] = value
                with self.assertRaises(RuntimeError):
                    cleanup.validate_bookings(changed)
        with self.assertRaises(RuntimeError):
            cleanup.validate_bookings(self.rows[:-1])

    def test_checkout_must_be_expired_unpaid_and_match(self):
        checkout = SimpleNamespace(status='expired', payment_status='unpaid', client_reference_id='a')
        cleanup.validate_session(self.rows[0], checkout)
        for field, value in [('status', 'open'), ('payment_status', 'paid'), ('client_reference_id', 'other')]:
            with self.subTest(field=field):
                changed = SimpleNamespace(**vars(checkout))
                setattr(changed, field, value)
                with self.assertRaises(RuntimeError):
                    cleanup.validate_session(self.rows[0], changed)


if __name__ == '__main__':
    unittest.main()
