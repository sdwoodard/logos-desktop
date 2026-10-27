#!/usr/bin/env python3
"""Install the latest stable LogosDesktop release or an explicitly verified wheel."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
import types
import urllib.parse
import urllib.request
import zipfile


RELEASES = "https://github.com/sdwoodard/logos-desktop/releases"
MANIFEST_URL = RELEASES + "/latest/download/controller-release.json"
MAX_WHEEL_BYTES = 64 * 1024 * 1024


def require_https(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.fragment or not parsed.hostname):
        raise ValueError("Downloads require HTTPS URLs without credentials or fragments")


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit):
    require_https(url)
    opener = urllib.request.build_opener(Redirects())
    started = time.monotonic()
    chunks = []
    size = 0
    with opener.open(url, timeout=10) as response:
        while True:
            block = response.read1(min(65536, limit + 1 - size))
            if time.monotonic() - started > 60:
                raise ValueError("Download exceeded its time limit")
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError("Download exceeded its size limit")
            chunks.append(block)
    return b"".join(chunks)


def latest_release():
    data = json.loads(download(MANIFEST_URL, 16 * 1024))
    if not isinstance(data, dict) or set(data) != {"version", "filename", "size", "sha256", "url"}:
        raise ValueError("Latest release metadata is incomplete")
    version = data["version"]
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("Latest release must name a stable version")
    filename = f"logos_desktop-{version}-py3-none-any.whl"
    if data["filename"] != filename or data["url"] != f"{RELEASES}/download/v{version}/{filename}":
        raise ValueError("Latest release must name the matching project wheel URL")
    if (type(data["size"]) is not int or not 0 < data["size"] <= MAX_WHEEL_BYTES
            or not isinstance(data["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", data["sha256"]) is None):
        raise ValueError("Latest release must provide a bounded size and SHA-256")
    return data


def configure_updates(root):
    prefix = (root / "current").resolve()
    command = [str(prefix / "bin/python"), "-I", "-m", "logos_desktop.controller_install", "configure-provider"]
    retry = "Controller installed, but update setup failed. Retry with: " + shlex.join(command)
    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        raise RuntimeError(retry) from error
    if result.returncode:
        raise RuntimeError(retry)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--latest", action="store_true", help="Install the latest stable project release")
    selection.add_argument("--wheel", help="Explicit local path or HTTPS URL")
    parser.add_argument("--root", help="Controller root, defaults to ~/.local/lib/logos-desktop with --latest")
    parser.add_argument("--version")
    parser.add_argument("--sha256")
    parser.add_argument("--size", type=int)
    parser.add_argument("--migrate", action="store_true", help="Migrate an explicitly selected managed installation")
    parser.add_argument("--config", help="Absolute existing profile path, required for migration")
    parser.add_argument("--frontend", choices=("qt", "tui"), default="qt")
    args = parser.parse_args(argv)
    if args.latest:
        if any(value is not None for value in (args.version, args.sha256, args.size, args.config)) or args.migrate:
            parser.error("--latest cannot be combined with explicit wheel metadata or migration")
        if sys.version_info < (3, 12):
            parser.error("Install Python 3.12 or newer before running the installer")
        if sys.platform != "linux" or platform.machine() != "x86_64":
            parser.error("The latest-release installer supports Linux x86_64")
        if os.geteuid() == 0:
            parser.error("Run the installer as your ordinary user, without sudo")
        root = Path(args.root).expanduser() if args.root else Path.home() / ".local/lib/logos-desktop"
        if not root.is_absolute():
            parser.error("Choose an absolute controller root")
        if root.exists() or root.is_symlink():
            parser.error("Controller root already exists. Use Home to update it, or choose a new --root")
        args.root = str(root)
        print("Checking the latest stable LogosDesktop release…", flush=True)
        data = latest_release()
        args.wheel, args.version, args.sha256, args.size = data["url"], data["version"], data["sha256"], data["size"]
    elif any(value is None for value in (args.root, args.version, args.sha256, args.size)):
        parser.error("Explicit wheels require --root, --version, --sha256 and --size")

    if (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", args.version) is None
            or re.fullmatch(r"[0-9a-f]{64}", args.sha256) is None
            or not 0 < args.size <= MAX_WHEEL_BYTES):
        parser.error("An exact version, SHA-256 and bounded wheel size are required")
    filename = f"logos_desktop-{args.version}-py3-none-any.whl"
    url = urllib.parse.urlsplit(args.wheel)
    if url.scheme:
        content = download(args.wheel, args.size)
    else:
        with Path(args.wheel).open("rb") as source:
            content = source.read(args.size + 1)
    if len(content) != args.size or hashlib.sha256(content).hexdigest() != args.sha256:
        raise ValueError("Wheel failed size or SHA-256 validation")

    # Import only the verified implementation, never arbitrary cwd package code.
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        info = archive.getinfo("logos_desktop/controller_install.py")
        if info.file_size > 256 * 1024:
            raise ValueError("Packaged installation implementation is too large")
        source = archive.read(info)
    module = types.ModuleType("_logos_desktop_verified_install")
    sys.modules[module.__name__] = module
    exec(compile(source, filename + "/logos_desktop/controller_install.py", "exec"), module.__dict__)
    with tempfile.TemporaryDirectory(prefix="logos-desktop-bootstrap-") as directory:
        wheel = Path(directory) / filename
        wheel.write_bytes(content)
        arguments = ["--root", args.root, "--wheel", str(wheel), "--version", args.version,
                     "--sha256", args.sha256, "--size", str(args.size)]
        if args.migrate:
            if not args.config:
                parser.error("Migration requires --config with an explicit existing profile")
            arguments.extend(["--migrate", "--config", args.config, "--frontend", args.frontend])
        result = module.bootstrap(arguments)
        if result == 0 and args.latest:
            configure_updates(root)
            print("Updates are configured. Open LogosDesktop with:\n" + shlex.join([str(root / "logos-desktop")]))
        return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        sys.exit(1)
