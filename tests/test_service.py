import os
import unittest
from unittest.mock import Mock, patch

import service


class ServiceTests(unittest.TestCase):
    def test_worker_failure_stops_web_and_exits_unsuccessfully(self):
        web, worker = Mock(), Mock()
        web.poll.return_value = None
        worker.poll.return_value = 1
        with patch.dict(os.environ, {'BALANCE_WORKER_IN_WEB': '1'}), \
                patch.object(service.signal, 'signal'), \
                patch.object(service.subprocess, 'Popen', side_effect=[web, worker]):
            self.assertEqual(service.main(), 1)
        web.terminate.assert_called_once()
        web.wait.assert_called_once()
        worker.wait.assert_called_once()

    def test_default_executes_only_web(self):
        with patch.dict(os.environ, {'BALANCE_WORKER_IN_WEB': '0'}), \
                patch.object(service.os, 'execvp', side_effect=SystemExit(0)) as run, \
                patch.object(service.subprocess, 'Popen') as spawn:
            with self.assertRaises(SystemExit):
                service.main()
        run.assert_called_once_with(service.WEB_COMMAND[0], service.WEB_COMMAND)
        spawn.assert_not_called()
