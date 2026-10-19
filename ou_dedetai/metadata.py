"""Bound metadata transport in an owned process, including slow response headers."""

from base64 import b64decode, b64encode
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import requests

from . import artifacts, processes


MAX_BYTES = 8 * 1024 * 1024
TOTAL_TIMEOUT = 45


def read(url):
    """Read a bounded body. The supervising process enforces the elapsed deadline."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username is not None:
        raise artifacts.MetadataError("Metadata requires credential-free HTTPS", "unavailable")
    try:
        with requests.get(url, stream=True, timeout=artifacts.NETWORK_TIMEOUT,
                          headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code in (403, 429):
                retry = response.headers.get("Retry-After", "")
                valid_retry = isinstance(retry, str) and retry.isdecimal() and len(retry) < 8
                retry = min(int(retry), 86400) if valid_retry else 300
                raise artifacts.MetadataError("Release service rate limit or access restriction", "rate-limited", retry)
            if response.status_code == 404:
                raise artifacts.MetadataError("Release service is unavailable or private", "unavailable")
            artifacts.require_response(response, url)
            size, _ = artifacts.response_metadata(response.headers)
            if size is not None and size > MAX_BYTES:
                raise artifacts.MetadataError("Metadata exceeds its body limit", "unavailable")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise artifacts.MetadataError("Metadata exceeds its body limit", "unavailable")
            return bytes(content)
    except requests.RequestException as error:
        raise artifacts.MetadataError(f"Metadata request failed ({type(error).__name__})") from None


def worker(url):
    try:
        result = {"status": "ok", "data": b64encode(read(url)).decode("ascii")}
    except artifacts.MetadataError as error:
        result = {"status": error.status, "message": str(error), "retry": error.retry_after}
    except Exception as error:
        result = {"status": "unavailable", "message": f"Metadata helper failed ({type(error).__name__})"}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


def request(url):
    from . import updates

    if processes.cancelled():
        raise artifacts.MetadataError("Release check cancelled")
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--fetch-metadata", url]
    else:
        root = str(Path(__file__).resolve().parent.parent)
        bootstrap = ('import runpy,sys\n'
                     'sys.path.insert(0,sys.argv.pop(1))\n'
                     'runpy.run_module("ou_dedetai.metadata",run_name="__main__")\n')
        command = [sys.executable, "-I", "-B", "-c", bootstrap, root, url]
    env = updates.fresh_environment()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        output, status = processes.capture(command, env=env, timeout=TOTAL_TIMEOUT,
                                           limit=MAX_BYTES * 2 + 4096, label="Metadata request")
        if status != 0:
            raise artifacts.MetadataError("Metadata helper did not complete")
        result = json.loads(output)
        if result["status"] != "ok":
            raise artifacts.MetadataError(result.get("message", "Metadata is unavailable"),
                                          result["status"], result.get("retry", 0))
        return b64decode(result["data"], validate=True)
    except processes.CaptureError as error:
        raise artifacts.MetadataError(str(error)) from error
    except (ValueError, KeyError, TypeError) as error:
        raise artifacts.MetadataError(f"Metadata helper failed ({type(error).__name__})") from None


if __name__ == "__main__":
    worker(sys.argv[1])
