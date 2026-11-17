"""Terminal input must not interrupt settlement of an owned offline inspection."""

import os
import pty
import unittest
from unittest.mock import Mock, patch

from logos_desktop import maintenance, runtime_commands


class InspectionInputTests(unittest.TestCase):
    def test_open_terminal_can_remain_idle_then_restart_and_retry_normal_stop(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        terminal = self.enterContext(os.fdopen(slave, 'r'))
        recovery = Mock()
        recovery.status.return_value = {'point': 'old'}

        def inspect(*, observe, status):
            record = {'id': 'copy', 'worker': {'phase': 'running'}, 'restarts': 0}
            status(record)
            recovery.restart_helper.assert_not_called()
            os.write(master, b'restart\n')
            status(record)
            recovery.restart_helper.assert_called_once_with('inspection-copy')
            status(dict(record, restarts=1))
            os.write(master, b'stop\n')
            status(record)
            recovery.stop_helper.assert_called_once_with('inspection-copy')
            os.write(master, b'stop\n')
            status(dict(record, worker={'phase': 'unknown'}))
            self.assertEqual(recovery.stop_helper.call_count, 2)
            raise maintenance.RecoveryRequired('Unknown helper settlement')

        recovery.inspect_generation.side_effect = inspect
        confirm = Mock()
        with patch.object(runtime_commands.sys, 'stdin', terminal), self.assertRaises(maintenance.RecoveryRequired):
            runtime_commands.inspect_point(recovery, 'old', confirm)
        confirm.assert_not_called()
