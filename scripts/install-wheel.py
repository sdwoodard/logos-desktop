#!/usr/bin/env python3
"""Bootstrap from an explicitly selected, SHA-256 verified LogosDesktop wheel.

There is no discovery feed. Obtain the digest through a trusted release channel.
The verified wheel supplies the installation implementation.
"""

import argparse
import hashlib
import io
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import urllib.parse
import urllib.request
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--wheel", required=True, help="Local path or HTTPS URL")
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", required=True, type=int)
    args = parser.parse_args()
    if (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", args.version) is None
            or re.fullmatch(r"[0-9a-f]{64}", args.sha256) is None
            or not 0 < args.size <= 64 * 1024 * 1024):
        parser.error("An exact version, SHA-256 and bounded wheel size are required")
    filename = f"logos_desktop-{args.version}-py3-none-any.whl"
    url = urllib.parse.urlsplit(args.wheel)
    if url.scheme:
        if url.scheme != "https" or url.username or url.password or url.fragment or not url.hostname:
            parser.error("Wheel retrieval requires an HTTPS URL without credentials")

        class Redirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                parsed = urllib.parse.urlsplit(newurl)
                if parsed.scheme != "https" or parsed.username or parsed.password:
                    raise ValueError("Wheel redirect requires HTTPS without credentials")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(Redirects())
        started = time.monotonic()
        chunks = []
        size = 0
        with opener.open(args.wheel, timeout=10) as response:
            while True:
                block = response.read1(min(65536, args.size + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > args.size or time.monotonic() - started > 60:
                    raise ValueError("Wheel transfer exceeded its size or time limit")
                chunks.append(block)
        content = b"".join(chunks)
    else:
        with Path(args.wheel).open("rb") as source:
            content = source.read(args.size + 1)
    if len(content) != args.size or hashlib.sha256(content).hexdigest() != args.sha256:
        raise ValueError("Wheel failed size or SHA-256 validation")

    # Import only the verified implementation, never arbitrary cwd package code.
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        info = archive.getinfo("ou_dedetai/controller_install.py")
        if info.file_size > 256 * 1024:
            raise ValueError("Packaged installation implementation is too large")
        source = archive.read(info)
    module = types.ModuleType("_logos_desktop_verified_install")
    sys.modules[module.__name__] = module
    exec(compile(source, filename + "/ou_dedetai/controller_install.py", "exec"), module.__dict__)
    with tempfile.TemporaryDirectory(prefix="logos-desktop-bootstrap-") as directory:
        wheel = Path(directory) / filename
        wheel.write_bytes(content)
        return module.bootstrap(["--root", args.root, "--wheel", str(wheel), "--version", args.version,
                                 "--sha256", args.sha256, "--size", str(args.size)])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        sys.exit(1)
