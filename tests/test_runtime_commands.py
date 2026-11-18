"""Terminal input must not interrupt settlement of an owned offline inspection."""

import os
import pty
import unittest
from unittest.mock import Mock, patch

from logos_desktop import maintenance, runtime_commands


class InspectionInputTests(unittest.TestCase):
    def test_terminal_experimental_opt_in_binds_target_and_preserves_trial_choice(self):
        conf = Mock()
        context = Mock()
        target = next(iter(runtime_commands.runtime_stacks.bundled().stacks))
        with (patch.object(maintenance.Context, "from_app", return_value=context),
              patch.object(runtime_commands, "apply_stack") as apply):
            with self.assertRaisesRegex(maintenance.MaintenanceError, "not confirmed"):
                runtime_commands.execute("apply", target, conf, experimental=True, confirm=lambda _: "yes")
            apply.assert_not_called()
            confirm = Mock(return_value="experimental " + target)
            runtime_commands.execute("apply", target, conf, experimental=True, skip_trial=True, confirm=confirm)
            args, options = apply.call_args
            self.assertEqual((args[0], args[1], args[3], args[4], args[5]), (context, conf, target, confirm, True))
            self.assertEqual(options, {"experimental": True})

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
