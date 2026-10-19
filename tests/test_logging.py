import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ou_dedetai import constants, msg, system, wine


class TestLoggingAndEnvironment(unittest.TestCase):
    def test_two_log_switches_keep_handler_policy_and_failure_keeps_usable_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = msg.GzippedRotatingFileHandler(root / "first.log", maxBytes=10000, backupCount=2)
            handler.name = "logfile"
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            logger = logging.Logger("fixture", logging.DEBUG)
            logger.manager = logging.Manager(logger)
            logger.addHandler(handler)
            try:
                with patch.object(logging, "getLogger", return_value=logger):
                    logger.info("first")
                    for index in (2, 3):
                        old_stream = handler.stream
                        msg.update_log_path(root / f"new-{index}/current.log")
                        self.assertTrue(old_stream.closed)
                        logger.info(f"message {index}")
                    previous = handler.stream
                    with patch.object(Path, "mkdir", side_effect=PermissionError("denied parent")):
                        with self.assertRaises(PermissionError):
                            msg.update_log_path(root / "denied/current.log")
                    self.assertIs(handler.stream, previous)
                    logger.info("after failure")
                    handler.flush()
                self.assertEqual((root / "first.log").read_text(), "INFO first\n")
                self.assertEqual((root / "new-2/current.log").read_text(), "INFO message 2\n")
                self.assertEqual((root / "new-3/current.log").read_text(), "INFO message 3\nINFO after failure\n")
                self.assertEqual((handler.maxBytes, handler.backupCount), (10000, 2))
                self.assertEqual(logger.handlers, [handler])
            finally:
                handler.close()

    def test_loader_cleanup_is_repeatable_and_both_wine_adapters_receive_restored_paths(self):
        for original in (None, "", "/custom lib/文"):
            env = {"LD_LIBRARY_PATH": "/bundle", "LIBPATH": "/bundle"}
            if original is not None:
                env.update(LD_LIBRARY_PATH_ORIG=original, LIBPATH_ORIG=original)
            before = env.copy()
            with patch.object(constants, "RUNMODE", "binary"):
                clean = system.fix_ld_library_path(env)
                self.assertEqual(system.fix_ld_library_path(clean), clean)
                self.assertEqual(env, before)
                for name in ("LD_LIBRARY_PATH", "LIBPATH"):
                    self.assertEqual(clean.get(name), original)

                with (patch.object(wine, "_wine_invocation", return_value=(["/fixture/wine"], clean)),
                      patch.object(wine.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run):
                    wine.run_wine_completed_process.__wrapped__(None, "/fixture/wine")
                self.assertEqual(run.call_args.kwargs["env"], clean)
                with patch.object(system.subprocess, "Popen") as popen:
                    system.popen_command(["/fixture/wine"], env=clean)
                self.assertEqual(popen.call_args.kwargs["env"], clean)
