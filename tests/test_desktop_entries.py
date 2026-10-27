from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from logos_desktop import constants, desktop, installer


class TestDesktopEntries(unittest.TestCase):
    def test_all_binary_entries_preserve_complex_launcher_and_direct_url_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "Logos Space 文"
            install.mkdir()
            app = SimpleNamespace(conf=SimpleNamespace(
                install_dir=str(install), faithlife_product="Logos",
                logos_exe_windows_path=r"C:\Logos Space\Logos.exe",
                _raw=SimpleNamespace(install_dir=str(install), faithlife_product="Logos"),
                _overrides=SimpleNamespace(install_dir=None, wine_prefix=None,
                                           config_path=str(root / 'chosen.json'), config_destination=None),
            ))
            with (
                patch.multiple(constants, RUNMODE="binary", XDG_DATA_HOME=str(root / "share"),
                               CONFIG_BASE=root / 'config', COORDINATION_DIR=root / 'coordination'),
                patch.object(desktop, '_default', return_value=None),
                patch.object(installer.system, "run_command") as commands,
            ):
                installer.create_launcher_shortcuts(app)
            entries = root / "share/applications"
            launcher = str(install / constants.BINARY_NAME)
            controller = next(path for path in entries.glob('*.desktop')
                              if not path.stem.endswith(('-app', '-url'))).read_text()
            product = next(entries.glob('*-app.desktop')).read_text()
            handler = next(entries.glob('*-url.desktop')).read_text()
            route = f'/usr/bin/env "--" "{launcher}" "-c" "{root / "chosen.json"}"'
            self.assertIn(f'Exec={route}\n', controller)
            self.assertIn(f'Exec={route} "--run-installed-app"\n', product)
            self.assertIn('"--wine" ', handler)
            self.assertIn(' %u\n', handler)
            self.assertIn("Terminal=false", handler)
            self.assertNotIn("/bin/sh", handler)
            self.assertTrue(all(isinstance(call.args[0], list) for call in commands.call_args_list))

    def test_invalid_paths_fail_before_writes_or_registration(self):
        for name in ("invalid=launcher", "invalid\nlauncher"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                install = root / name
                install.mkdir()
                app = SimpleNamespace(conf=SimpleNamespace(
                    install_dir=str(install), faithlife_product="Logos", logos_exe_windows_path=r"C:\Logos.exe",
                    _raw=SimpleNamespace(install_dir=str(install), faithlife_product="Logos"),
                    _overrides=SimpleNamespace(install_dir=None, wine_prefix=None,
                                               config_path=str(root / 'chosen.json'), config_destination=None),
                ))
                with (
                    patch.multiple(constants, RUNMODE="binary", XDG_DATA_HOME=str(root / "share")),
                    patch.object(installer.system, "run_command") as commands,
                    self.assertRaises(ValueError),
                ):
                    installer.create_launcher_shortcuts(app)
                commands.assert_not_called()
                self.assertEqual(list(install.iterdir()), [])
                self.assertFalse((root / "share").exists())

    def test_script_mode_uses_current_interpreter_and_script_as_separate_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "Source 文/main script.py"
            install = root / "install"
            install.mkdir()
            app = SimpleNamespace(conf=SimpleNamespace(
                install_dir=str(install), faithlife_product="Logos", logos_exe_windows_path=None,
                _raw=SimpleNamespace(install_dir=str(install), faithlife_product="Logos"),
                _overrides=SimpleNamespace(install_dir=None, wine_prefix=None,
                                           config_path=str(root / 'chosen.json'), config_destination=None),
            ))
            with (
                patch.multiple(constants, RUNMODE="script", XDG_DATA_HOME=str(root / "share")),
                patch.object(installer.sys, "argv", [str(script)]),
                patch.object(installer.sys, "executable", str(root / "Python Space/bin/python")),
                patch.object(desktop, 'preview', return_value=Mock()) as preview,
                patch.object(desktop, 'adopt', return_value=SimpleNamespace(issues=())),
            ):
                installer.create_launcher_shortcuts(app)
            self.assertEqual(preview.call_args.args[1], [
                str(root / "Python Space/bin/python"), str(script),
            ])

    def test_atomic_desktop_replacement_preserves_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = root / "applications"
            entries.mkdir()
            sentinel = root / "outside"
            sentinel.write_bytes(b"unrelated")
            (entries / "fixture.desktop").symlink_to(sentinel)
            with patch.object(constants, "XDG_DATA_HOME", str(root)):
                result = installer.create_desktop_file("fixture.desktop", "Fixture", ["/fixture/with space"])
            self.assertEqual(sentinel.read_bytes(), b"unrelated")
            self.assertFalse(result.is_symlink())

    def test_url_field_is_distinct_from_literal_percent_arguments(self):
        command = installer.desktop_exec(["/fixture/100% launcher", "literal%u"], accepts_url=True)
        self.assertEqual(command, '/usr/bin/env "--" "/fixture/100%% launcher" "literal%%u" %u')

    def test_safe_executable_is_bare_for_native_kde_lookup(self):
        for executable in ("/usr/bin/python", "python3", "/opt/Logos-Desktop_1.2+3/run"):
            for accepts_url in (False, True):
                with self.subTest(executable=executable, accepts_url=accepts_url):
                    command = installer.desktop_exec(
                        [executable, "", "literal%u %f %U %i"], accepts_url=accepts_url,
                    )
                    expected = f'{executable} "" "literal%%u %%f %%U %%i"'
                    self.assertEqual(command, expected + (" %u" if accepts_url else ""))

    def test_complex_executable_uses_bare_env_and_preserves_escaped_argument(self):
        for character in (" ", "文", "%", "'", '"', "$", "`", "\\", ";", "&", "(", ":"):
            with self.subTest(character=character):
                executable = f"/fixture/{character}launcher"
                command = installer.desktop_exec([executable, "argument"])
                self.assertTrue(command.startswith('/usr/bin/env "--" "'))
                # The executable is now an ordinary argument with identical escaping.
                escaped_argument = installer.desktop_exec(["/safe", executable, "argument"])
                self.assertEqual(command.removeprefix('/usr/bin/env "--" '),
                                 escaped_argument.removeprefix("/safe "))

    def test_invalid_executable_and_control_arguments_are_rejected(self):
        for arguments in ([], [""], ["invalid=launcher"], ["/safe", "bad\nargument"],
                          ["/space launcher", "bad\x00argument"], ["/bad\tlauncher"],
                          ["/safe", "bad\x7fargument"]):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                installer.desktop_exec(arguments, accepts_url=True)
