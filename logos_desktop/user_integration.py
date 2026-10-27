"""Owned command, desktop-menu and shell PATH setup for one controller installation."""

import json
import os
from pathlib import Path
import pwd
import shlex
import stat

from . import constants, controller_install, file_transactions, paths, runtime


START = b"# >>> LogosDesktop PATH >>>"
END = b"# <<< LogosDesktop PATH <<<"
POSIX_BLOCK = b'''# >>> LogosDesktop PATH >>>
case ":${PATH}:" in
  *":${HOME}/.local/bin:"*) ;;
  *) export PATH="${HOME}/.local/bin${PATH:+:${PATH}}" ;;
esac
# <<< LogosDesktop PATH <<<
'''
FISH_BLOCK = b'''# >>> LogosDesktop PATH >>>
if not contains -- "$HOME/.local/bin" $PATH
    set -gx PATH "$HOME/.local/bin" $PATH
end
# <<< LogosDesktop PATH <<<
'''


def command_content(root):
    return f'#!/bin/sh\nexec {shlex.quote(str(root / "logos-desktop"))} "$@"\n'.encode()


def command_matches(root, candidate):
    """Admit only the exact user-command wrapper for this installation."""
    path = Path(candidate).absolute()
    if path != Path.home() / ".local/bin/logos-desktop":
        return False
    try:
        content = file_transactions.read_owned(path, limit=4096)
        return content == command_content(root) and stat.S_IMODE(path.lstat().st_mode) == 0o700
    except (OSError, ValueError, RuntimeError):
        return False


def shell_files(home):
    shell = Path(os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell).name
    if shell == "bash":
        login = next((home / name for name in (".bash_profile", ".bash_login")
                      if os.path.lexists(home / name)), home / ".profile")
        return [(home / ".bashrc", POSIX_BLOCK), (login, POSIX_BLOCK)]
    if shell == "zsh":
        directory = Path(os.environ.get("ZDOTDIR") or home)
        if not directory.is_absolute() or not directory.is_relative_to(home):
            raise ValueError("ZDOTDIR must be an absolute directory inside your home, or use --no-path")
        return [(directory / ".zshrc", POSIX_BLOCK)]
    if shell == "fish":
        directory = paths.xdg_base(os.environ.get("XDG_CONFIG_HOME"), home / ".config")
        return [(directory / "fish/conf.d/logos-desktop.fish", FISH_BLOCK)]
    raise ValueError(f"Automatic PATH setup does not support {shell}. Retry setup with --no-path")


def path_content(previous, block):
    previous = previous or b""
    if START in previous or END in previous:
        if previous.count(START) != 1 or previous.count(END) != 1 or block not in previous:
            raise ValueError("The LogosDesktop PATH block was edited. Preserve it or use --no-path")
        return previous
    return previous + (b"\n" if previous and not previous.endswith(b"\n") else b"") + b"\n" + block


def _parents(path, *, create=False):
    runtime._safe_target(path)
    for parent in reversed(path.parents):
        created = False
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if not create:
                continue
            parent.mkdir(mode=0o700)
            info = parent.lstat()
            created = True
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
            raise ValueError(f"Unsafe launcher setup parent: {parent}")
        if created:
            controller_install._sync(parent)
            controller_install._sync(parent.parent)


def _valid_mode(value):
    return type(value) is int and 0 < value <= 0o777 and not value & 0o022


def _record(journal, root, allowed):
    content = file_transactions.read_owned(journal, limit=8 * file_transactions.MAX_CONFIG_BYTES)
    if content is None:
        return None
    record = json.loads(content)
    if (not isinstance(record, dict) or set(record) - {"schema", "root", "state", "files", "conflicts"}
            or type(record.get("schema")) is not int or record["schema"] != 1 or record.get("root") != str(root)
            or record.get("state") not in ("prepared", "committed", "rolled-back", "conflict")
            or not isinstance(record.get("files"), list) or not 1 <= len(record["files"]) <= 6):
        raise ValueError("Invalid launcher setup journal")
    names = []
    encoded_limit = 4 * ((file_transactions.MAX_CONFIG_BYTES + 2) // 3)
    for item in record["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "before", "after", "before_mode", "mode"}
                or not isinstance(item["path"], str)):
            raise ValueError("Invalid launcher journal file record")
        names.append(item["path"])
        if ((item["before"] is None) != (item["before_mode"] is None)
                or not _valid_mode(item["mode"])
                or (item["before_mode"] is not None and not _valid_mode(item["before_mode"]))):
            raise ValueError("Invalid launcher journal file mode")
        for field in ("before", "after"):
            value = item[field]
            if value is None and field == "before":
                continue
            if not isinstance(value, str) or len(value) > encoded_limit:
                raise ValueError("Invalid launcher journal file content")
            try:
                decoded = file_transactions.decode_content(value)
            except ValueError as error:
                raise ValueError("Invalid launcher journal file encoding") from error
            if len(decoded) > file_transactions.MAX_CONFIG_BYTES:
                raise ValueError("Launcher journal file content exceeds its size limit")
    if len(names) != len(set(names)) or not set(names).issubset(allowed):
        raise ValueError("Launcher setup context changed. Recover with the original HOME, XDG and shell settings")
    conflicts = record.get("conflicts", [])
    if (not isinstance(conflicts, list) or any(not isinstance(path, str) or path not in names for path in conflicts)
            or len(conflicts) != len(set(conflicts)) or bool(conflicts) != (record["state"] == "conflict")):
        raise ValueError("Invalid launcher journal conflict state")
    return record


def install(root, *, modify_path=True):
    from .installer import desktop_exec

    root = controller_install._root(root)
    home = Path.home()
    if ":" in str(home) or any(ord(character) < 32 for character in str(home)):
        raise ValueError("The home directory cannot contain PATH separators or control characters")
    binary = home / ".local/bin/logos-desktop"
    data = paths.xdg_base(os.environ.get("XDG_DATA_HOME"), home / ".local/share")
    desktop = data / "applications/logos-desktop.desktop"
    icon = root / "logos-desktop.svg"
    icon_value = str(icon).replace("\\", "\\\\")
    entry = ("[Desktop Entry]\nType=Application\nName=LogosDesktop\n"
             "GenericName=Logos installer and manager\nComment=Install and manage Logos on Linux through Wine\n"
             f"Exec={desktop_exec([str(root / 'logos-desktop'), '--dialog', 'qt'])}\n"
             f"Icon={icon_value}\n"
             "Terminal=false\nCategories=Education;\nKeywords=Logos;Bible;Wine;\n").encode()
    fixed = {binary: (command_content(root), 0o700), desktop: (entry, 0o600),
             icon: ((Path(constants.APP_IMAGE_DIR) / "logos-desktop.svg").read_bytes(), 0o600)}
    in_path = str(binary.parent) in os.environ.get("PATH", "").split(os.pathsep)
    try:
        known_startup = shell_files(home)
    except ValueError:
        if modify_path and not in_path:
            raise
        known_startup = []
    startup = known_startup if modify_path and not in_path else []
    # Include known startup locations during recovery even if PATH is already set.
    allowed = {str(path) for path in fixed}
    allowed.update(str(path) for path, _ in known_startup)
    allowed.update(str(home / name) for name in (".bashrc", ".profile", ".bash_profile", ".bash_login", ".zshrc"))
    journal = root / "launchers.json"
    from .desktop import integration_lock

    with controller_install._locked(root), integration_lock():
        if (root / "operation.json").exists():
            operation = controller_install._receipt(root)
            if operation["status"] not in ("ready", "rolled-back", "aborted"):
                raise ValueError("Reconcile the pending controller update before setting up launchers")
        previous_journal = file_transactions.read_owned(journal, limit=8 * file_transactions.MAX_CONFIG_BYTES)
        prior = _record(journal, root, allowed)
        if prior is not None and prior["state"] in ("prepared", "conflict"):
            conflicts = file_transactions.rollback_files(journal, prior)
            if conflicts:
                raise ValueError("Launcher recovery preserved changed files: " + ", ".join(conflicts))
        owned = {}
        if prior is not None and prior["state"] == "committed":
            owned = {item["path"]: file_transactions.decode_content(item["after"]) for item in prior["files"]}
        changes = []
        targets = [(path, content, mode, False) for path, (content, mode) in fixed.items()]
        targets.extend((path, block, None, True) for path, block in startup)
        for path, content, mode, shell in targets:
            _parents(path)
            before = file_transactions.read_owned(path)
            before_mode = stat.S_IMODE(path.lstat().st_mode) if before is not None else None
            if before_mode is not None and (not _valid_mode(before_mode) or not before_mode & 0o200):
                raise ValueError(f"Unsupported existing file permissions: {path}")
            if shell:
                content = path_content(before, content)
                mode = before_mode or 0o600
            elif before not in (None, content, owned.get(str(path))):
                raise ValueError(f"Existing file belongs to another installation or was edited: {path}")
            if len(content) > file_transactions.MAX_CONFIG_BYTES:
                raise ValueError(f"Launcher setup file exceeds its size limit: {path}")
            changes.append({"path": str(path), "before": file_transactions.encode_content(before),
                            "after": file_transactions.encode_content(content),
                            "before_mode": before_mode, "mode": mode})
        record = {"schema": 1, "root": str(root), "state": "prepared", "files": changes}
        if any(item["before"] != item["after"] or item["before_mode"] != item["mode"] for item in changes):
            for path, _, _, _ in targets:
                _parents(path, create=True)
            try:
                file_transactions.publish_files(journal, record)
            except (OSError, ValueError, RuntimeError):
                current_journal = file_transactions.read_owned(journal, limit=8 * file_transactions.MAX_CONFIG_BYTES)
                if current_journal != previous_journal:
                    file_transactions.rollback_files(journal, record)
                raise
    message = "Command and application-menu entry installed. "
    if startup:
        return message + "Open a new terminal to use logos-desktop."
    if not in_path:
        return message + "Open LogosDesktop from your application menu, or add ~/.local/bin to PATH."
    return message + "Run logos-desktop to open the app."
