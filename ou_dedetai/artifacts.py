"""Acceptance and immutable publication of installer artifacts.

The transfer structure is informed by Stephen Woodard's original PR #487.
Transport checks, local byte identity and publisher digests are separate evidence.
"""

from base64 import b64decode
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
import lzma
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tarfile
import tempfile
from urllib.parse import urlsplit, urlunsplit
import zipfile
import zlib

import olefile
import requests
import zstandard

from . import maintenance, msi, paths
from .msi import MsiExpectation


NETWORK_TIMEOUT = (10, 30)
COMPANIONS = ("wine", "wine64", "wineserver", "winetricks")

# Request-library debug records include query values from signed download URLs.
logging.getLogger("urllib3").setLevel(logging.ERROR)


class ArtifactError(RuntimeError):
    """The operation did not produce bytes acceptable to its consumer."""


class MetadataError(ArtifactError):
    """Metadata is unavailable without implying a successfully empty feed."""

    def __init__(self, message, status="offline", retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Digest:
    algorithm: str
    value: str
    source: str

    def validate(self):
        lengths = {"sha256": 64, "md5": 32}
        length = lengths.get(self.algorithm) if isinstance(self.algorithm, str) else None
        if (length is None or not isinstance(self.value, str) or len(self.value) != length
                or not isinstance(self.source, str) or not self.source):
            raise ArtifactError("Invalid required artifact digest")
        try:
            bytes.fromhex(self.value)
        except ValueError as error:
            raise ArtifactError("Invalid required artifact digest") from error


@dataclass(frozen=True)
class ArtifactSpec:
    url: str
    name: str
    kind: str
    digest: Digest | None = None
    size: int | None = None
    msi_expectation: MsiExpectation | None = None

    def validate(self):
        if (not isinstance(self.url, str) or not self.url or not isinstance(self.name, str)
                or not self.name or Path(self.name).name != self.name or self.name in (".", "..")):
            raise ArtifactError("Artifact name must be a single filename")
        if self.kind not in ("appimage", "msi", "icu", "winetricks-zip", "winetricks", "elf"):
            raise ArtifactError("Artifact role is missing or unsupported")
        if self.digest is not None:
            if not isinstance(self.digest, Digest):
                raise ArtifactError("Invalid expected artifact digest")
            self.digest.validate()
        if self.size is not None and (type(self.size) is not int or self.size <= 0):
            raise ArtifactError("Invalid expected artifact size")
        if self.msi_expectation is not None:
            if self.kind != "msi" or not isinstance(self.msi_expectation, MsiExpectation):
                raise ArtifactError("Invalid MSI artifact expectation")
            try:
                self.msi_expectation.validate()
            except ValueError as error:
                raise ArtifactError(str(error)) from None

    @property
    def key(self):
        identity = f"{self.url}\n{self.name}\n{self.kind}"
        return hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class AcceptedArtifact:
    path: Path
    spec: ArtifactSpec
    sha256: str
    size: int
    evidence: str

    def check(self, checkpoint=None):
        self.spec.validate()
        if regular_hash(self.path, checkpoint=checkpoint) != self.sha256 or self.path.stat().st_size != self.size:
            raise ArtifactError(f"Accepted artifact changed: {self.path.name}")
        validate_format(self.path, self.spec.kind, checkpoint, msi_expectation=self.spec.msi_expectation)
        return self.path


def safe_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def regular_hash(path, algorithm="sha256", checkpoint=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactError("Artifact must be a regular file")
        digest = hashlib.new(algorithm, usedforsecurity=False)
        while True:
            if checkpoint:
                checkpoint()
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise ArtifactError("Artifact changed while being read")
        return digest.hexdigest()


def response_metadata(headers):
    """Parse optional hints without inventing checksum semantics for ETags."""
    size = None
    text = headers.get("Content-Length")
    if isinstance(text, str) and text.isascii() and text.isdecimal() and len(text) < 20:
        size = int(text)
    digest = None
    text = headers.get("Content-MD5")
    if isinstance(text, str):
        try:
            raw = b64decode(text.strip(), validate=True)
        except ValueError:
            pass
        else:
            if len(raw) == 16:
                digest = Digest("md5", raw.hex(), "HTTP Content-MD5")
    return size, digest


def require_response(response, url):
    if response.status_code != 200:
        raise ArtifactError(f"HTTP {response.status_code} from {safe_url(url)}")
    if not _secure_response(response):
        raise ArtifactError("Download redirect left HTTPS")


def _secure_response(response):
    return all(isinstance(item.url, str) and urlsplit(item.url).scheme == "https"
               for item in (*response.history, response))


def get_bytes(url):
    from . import metadata

    return metadata.request(url)


def head_metadata(url):
    try:
        with requests.head(
            url, allow_redirects=True, timeout=NETWORK_TIMEOUT, headers={"Accept-Encoding": "identity"},
        ) as response:
            if (response.status_code == 200 and _secure_response(response)
                    and response.headers.get("Content-Encoding", "identity") == "identity"):
                return response_metadata(response.headers)
    except requests.RequestException:
        pass
    return None, None


def _elf_extent(path):
    size = path.stat().st_size
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            raise ArtifactError("Expected a 64-bit little-endian ELF executable")
        values = struct.unpack("<16sHHIQQQIHHHHHH", header)
        _, kind, machine, version, entry, phoff, shoff, _, ehsize, phsize, phnum, shsize, shnum, _ = values
        if kind not in (2, 3) or machine != 62 or version != 1 or ehsize != 64 or not phnum:
            raise ArtifactError("Unsupported ELF architecture or executable header")
        if phsize != 56 or phnum > 4096 or phoff < 64 or phoff + phsize * phnum > size:
            raise ArtifactError("Invalid ELF program table")
        extent = phoff + phsize * phnum
        entry_mapped = False
        for index in range(phnum):
            source.seek(phoff + index * phsize)
            program = struct.unpack("<IIQQQQQQ", source.read(56))
            offset, length = program[2], program[5]
            if offset + length > size:
                raise ArtifactError("Truncated ELF segment")
            if program[0] == 1:
                if program[6] < length:
                    raise ArtifactError("Invalid ELF load segment size")
                entry_mapped |= bool(entry and program[1] & 1 and program[3] <= entry < program[3] + length)
            extent = max(extent, offset + length)
        if not entry_mapped:
            raise ArtifactError("ELF entry point is not mapped to executable file content")
        if shnum:
            if shsize != 64 or shnum > 65535 or shoff < 64 or shoff + shsize * shnum > size:
                raise ArtifactError("Invalid ELF section table")
            extent = max(extent, shoff + shsize * shnum)
            for index in range(shnum):
                source.seek(shoff + index * shsize)
                section = struct.unpack("<IIQQQQIIQQ", source.read(64))
                if section[1] != 8:
                    end = section[4] + section[5]
                    if end > size:
                        raise ArtifactError("Truncated ELF section")
                    extent = max(extent, end)
        return header, extent


def validate_appimage(path):
    header, extent = _elf_extent(path)
    if header[8:11] != b"AI\x02":
        raise ArtifactError("Expected a type-2 AppImage")
    with path.open("rb") as source:
        source.seek(extent)
        # Runtime padding is bounded. A magic string elsewhere is not a payload.
        padding = source.read(4096 + 96)
        offset = padding.find(b"hsqs")
        if offset < 0 or offset > 4096 or len(padding[offset:offset + 96]) != 96:
            raise ArtifactError("AppImage has no complete SquashFS superblock")
        (
            _, inode_count, _, block_size, _, compression, block_log, _, id_count, major, minor,
            root_inode, bytes_used, _, _, inode_table, directory_table, _, _,
        ) = struct.unpack("<5I6H8Q", padding[offset:offset + 96])
        if (
            not inode_count or not id_count or (major, minor) != (4, 0)
            or block_log not in range(12, 21) or block_size != 1 << block_log
            or bytes_used <= 96 or extent + offset + bytes_used > path.stat().st_size
        ):
            raise ArtifactError("Invalid or truncated AppImage SquashFS payload")
        if any(value < 96 or value >= bytes_used for value in (inode_table, directory_table)):
            raise ArtifactError("Invalid SquashFS metadata tables")
        _squashfs_root(source, extent + offset, bytes_used, compression, root_inode,
                       inode_table, directory_table, inode_count, id_count)


def _squashfs_root(source, start, size, compression, root, inode_table, directory_table, inodes, ids):
    """Read bounded root metadata, not just a superblock with plausible offsets."""
    def metadata(block, offset, length):
        if offset >= 8192 or length > 1024 * 1024:
            raise ArtifactError("SquashFS metadata request is out of bounds")
        output = bytearray()
        for _ in range(256):
            if block < 96 or block + 2 > size:
                raise ArtifactError("SquashFS metadata block is out of bounds")
            source.seek(start + block)
            header = source.read(2)
            if len(header) != 2:
                raise ArtifactError("Truncated SquashFS metadata")
            encoded = int.from_bytes(header, "little")
            count = encoded & 0x7fff
            if not count or count > 8192 or block + 2 + count > size:
                raise ArtifactError("Invalid SquashFS metadata length")
            data = source.read(count)
            if not encoded & 0x8000:
                if compression == 1:
                    decoder = zlib.decompressobj()
                    data = decoder.decompress(data, 8193)
                    complete = decoder.eof
                elif compression == 4:
                    decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
                    data = decoder.decompress(data, max_length=8193)
                    complete = decoder.eof
                elif compression == 6:
                    advertised = zstandard.frame_content_size(data)
                    unknown = (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR)
                    if advertised not in unknown and advertised > 8192:
                        raise ArtifactError("Oversized SquashFS metadata compression")
                    data = zstandard.ZstdDecompressor(max_window_size=8192).decompress(
                        data, max_output_size=8192, allow_extra_data=False,
                    )
                    complete = True
                else:
                    raise ArtifactError("Unsupported compressed SquashFS metadata")
                if not complete:
                    raise ArtifactError("Incomplete SquashFS metadata compression")
            if len(data) > 8192 or offset >= len(data):
                raise ArtifactError("Invalid SquashFS metadata offset")
            copied = min(length - len(output), len(data) - offset)
            output.extend(data[offset:offset + copied])
            if len(output) == length:
                return bytes(output)
            block += count + 2
            offset = 0
        raise ArtifactError("SquashFS metadata exceeds bounded inspection")

    def inode(reference, length=16):
        content = metadata(inode_table + (reference >> 16), reference & 0xffff, length)
        kind, _, uid, gid, _, number = struct.unpack("<4H2I", content[:16])
        if kind not in range(1, 15) or uid >= ids or gid >= ids or number not in range(1, inodes + 1):
            raise ArtifactError("Invalid SquashFS inode")
        return kind, content

    kind, _ = inode(root)
    if kind == 1:
        _, data = inode(root, 32)
        block, links, length, offset, _ = struct.unpack("<IIHHI", data[16:])
    elif kind == 8:
        _, data = inode(root, 40)
        links, length, block, _, _, offset, _ = struct.unpack("<4I2HI", data[16:])
    else:
        raise ArtifactError("SquashFS root is not a directory")
    if links < 2 or length <= 3:
        raise ArtifactError("AppImage has no root directory entries")
    directory = metadata(directory_table + block, offset, length - 3)
    cursor = 0
    names = set()
    while cursor < len(directory):
        count, child_block, number = struct.unpack_from("<3I", directory, cursor)
        cursor += 12
        if count > 255:
            raise ArtifactError("Invalid SquashFS directory entry count")
        for _ in range(count + 1):
            child_offset, difference, declared_kind, name_size = struct.unpack_from("<HhHH", directory, cursor)
            cursor += 8
            if name_size > 255 or cursor + name_size + 1 > len(directory):
                raise ArtifactError("Truncated SquashFS directory name")
            name = directory[cursor:cursor + name_size + 1]
            cursor += name_size + 1
            if name in names or name in (b".", b"..") or b"/" in name or b"\0" in name:
                raise ArtifactError("Invalid SquashFS directory name")
            names.add(name)
            if name == b"AppRun" and declared_kind not in (2, 3):
                raise ArtifactError("AppImage AppRun is not a file or symbolic link")
            actual_kind, child = inode((child_block << 16) | child_offset)
            if ((actual_kind - 1) % 7) + 1 != declared_kind or int.from_bytes(child[12:16], "little") != (
                number + difference
            ):
                raise ArtifactError("SquashFS directory inode mismatch")
    if b"AppRun" not in names:
        raise ArtifactError("AppImage root has no AppRun entry")


def _msi_name(name):
    return msi.stream_name(name)


@contextmanager
def _msi_database(path, checkpoint):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        before = msi.preflight(source, checkpoint)
        with olefile.OleFileIO(source, raise_defects=olefile.DEFECT_INCORRECT) as database:
            yield database
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise ArtifactError("MSI changed while being read")


def validate_msi(path, checkpoint=None, *, expectation=None):
    try:
        with _msi_database(path, checkpoint) as database:
            if database.root.clsid.upper() != "000C1084-0000-0000-C000-000000000046":
                raise ArtifactError("Expected a Windows Installer database")
            if (database.root.size > msi.METADATA_LIMIT
                    or database.num_mini_fat_sectors * database.sectorsize
                    > msi.METADATA_LIMIT // 16 + database.sectorsize):
                raise ArtifactError("Oversized MSI mini stream or MiniFAT")
            streams = database.listdir()
            if len(streams) > msi.STREAM_LIMIT:
                raise ArtifactError("MSI stream-object budget exceeded")
            names = {_msi_name(parts[0]): parts for parts in streams if len(parts) == 1}
            if len(names) != sum(len(parts) == 1 for parts in streams):
                raise ArtifactError("Duplicate MSI stream name")
            if not {"_StringPool", "_StringData", "_Tables", "_Columns"}.issubset(names):
                raise ArtifactError("MSI database tables are missing")
            if database.get_size("\x05SummaryInformation") > msi.HEADER_LIMIT:
                raise ArtifactError("Oversized MSI summary")
            with database.openstream("\x05SummaryInformation") as stream:
                summary = stream.read(msi.HEADER_LIMIT + 1)
            properties = msi.summary_information(summary, checkpoint)
            template = properties.get(7)
            if isinstance(template, bytes):
                template = template.decode("ascii", errors="strict")
            platform = template.split(";")[0] if isinstance(template, str) else None
            if platform != "x64" and not (platform == "Intel" and expectation is not None):
                raise ArtifactError("Expected an x64 MSI database")
            tables = msi.Database(database, names, checkpoint)
            if expectation is not None:
                if not isinstance(expectation, MsiExpectation):
                    raise ArtifactError("Invalid MSI artifact expectation")
                if re.fullmatch(r"(?:Intel|x64);[0-9]{1,5}", template) is None:
                    raise ArtifactError("Malformed MSI platform/language template")
                primary = msi.primary_file(tables, expectation)
                if platform == "Intel":
                    flags = properties.get(15, 0)
                    if not isinstance(flags, int) or flags < 0:
                        raise ArtifactError("Invalid MSI source flags")
                    msi.attest(tables, primary, flags)
            for parts in streams:
                if checkpoint:
                    checkpoint()
                if database.get_size(parts) >= database.minisectorcutoff:
                    stream = msi.SectorReader(database, parts, checkpoint)
                    for offset in range(0, stream.size, msi.HEADER_LIMIT):
                        stream.read(offset, min(msi.HEADER_LIMIT, stream.size - offset))
                else:
                    with database.openstream(parts) as stream:
                        stream.read()
    except (OSError, ValueError, LookupError, struct.error, zlib.error) as error:
        raise ArtifactError(f"Invalid MSI database: {error}") from None


def _archive_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ArtifactError("Unsafe archive member")
    return path


def winetricks_member(archive, checkpoint=None):
    candidates = []
    names = set()
    for member in archive.infolist():
        if checkpoint:
            checkpoint()
        path = _archive_name(member.filename)
        mode = member.external_attr >> 16
        if member.filename in names or (stat.S_IFMT(mode) and not (
            stat.S_ISREG(mode) or stat.S_ISDIR(mode)
        )):
            raise ArtifactError("Duplicate or linked ZIP member")
        names.add(member.filename)
        if not member.is_dir() and path.name == "winetricks":
            candidates.append(member)
        if not member.is_dir():
            with archive.open(member) as source:
                while source.read(1024 * 1024):
                    if checkpoint:
                        checkpoint()
    if len(candidates) != 1:
        raise ArtifactError("Winetricks archive must contain exactly one valid script")
    return candidates[0]


def validate_format(path, kind, checkpoint=None, *, msi_expectation=None):
    if checkpoint:
        checkpoint()
    if not path.stat().st_size:
        raise ArtifactError("Artifact is empty")
    try:
        if kind == "appimage":
            validate_appimage(path)
        elif kind == "elf":
            _elf_extent(path)
        elif kind == "msi":
            validate_msi(path, checkpoint, expectation=msi_expectation)
        elif kind == "winetricks-zip":
            with zipfile.ZipFile(path) as archive:
                member = winetricks_member(archive, checkpoint)
                with archive.open(member) as source:
                    if not source.read(32).startswith(b"#!/bin/sh"):
                        raise ArtifactError("Winetricks member is not a shell script")
        elif kind == "winetricks":
            with path.open("rb") as source:
                if not source.read(32).startswith(b"#!/bin/sh"):
                    raise ArtifactError("Expected a Winetricks shell script")
        elif kind == "icu":
            roots = set()
            with tarfile.open(path, "r:gz") as archive:
                for member in archive:
                    if checkpoint:
                        checkpoint()
                    name = _archive_name(member.name)
                    if not (member.isfile() or member.isdir()):
                        raise ArtifactError("ICU archive contains a linked or special member")
                    for root in (PurePosixPath("windows"), PurePosixPath("icu-win/windows")):
                        if member.isfile() and name.is_relative_to(root):
                            roots.add(str(root))
                    if member.isfile():
                        with archive.extractfile(member) as source:
                            while source.read(1024 * 1024):
                                if checkpoint:
                                    checkpoint()
            if len(roots) != 1:
                raise ArtifactError("ICU archive must have one supported Windows subtree")
    except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile,
            struct.error, zlib.error, lzma.LZMAError, zstandard.ZstdError) as error:
        raise ArtifactError(f"Invalid {kind} artifact ({type(error).__name__})") from None


def _verify(path, spec, size=None, digest=None, checkpoint=None):
    actual_size = path.stat().st_size
    if any(expected is not None and actual_size != expected for expected in (spec.size, size)):
        raise ArtifactError("Artifact length mismatch")
    for expected in (spec.digest, digest):
        if expected is not None:
            expected.validate()
            if regular_hash(path, expected.algorithm, checkpoint).lower() != expected.value.lower():
                raise ArtifactError("Artifact digest mismatch")
    validate_format(path, spec.kind, checkpoint, msi_expectation=spec.msi_expectation)


def _store(root):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ArtifactError("Artifact destination must be absolute")
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ArtifactError("Artifact destination contains a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    return maintenance.private_directory(root / ".artifacts")


def _publish(staging, store, spec, evidence, checkpoint=None):
    file = staging / spec.name
    sha256 = regular_hash(file, checkpoint=checkpoint)
    parent = maintenance.private_directory(store / spec.key)
    destination = parent / sha256
    file.chmod(0o500 if spec.kind in ("appimage", "elf", "winetricks") else 0o400)
    if spec.kind == "appimage":
        for name in COMPANIONS:
            if name != spec.name:
                (staging / name).symlink_to(spec.name)
    if checkpoint:
        checkpoint()
    # Publication is the commit boundary. Finish acceptance after a late cancel.
    try:
        paths.rename_no_replace(staging, destination)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_dir():
            raise ArtifactError("Conflicting artifact generation")
        if regular_hash(destination / spec.name) != sha256:
            raise ArtifactError("Conflicting artifact generation contents")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    accepted = AcceptedArtifact(
        destination / spec.name, spec, sha256, (destination / spec.name).stat().st_size, evidence,
    )
    accepted.check()
    return accepted


@contextmanager
def _staged(root, spec):
    spec.validate()
    with maintenance.artifact_use():
        store = _store(root)
        temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=store))
        try:
            yield temporary, store
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def import_local(source, root, spec):
    """Snapshot an explicitly selected local artifact without remote provenance."""
    try:
        with _staged(root, spec) as (staging, store):
            source = Path(source)
            expected = regular_hash(source)
            previous = store / spec.key / expected / spec.name
            if previous.exists():
                accepted = AcceptedArtifact(previous, spec, expected, source.stat().st_size, "explicit local selection")
                accepted.check()
                return accepted
            with source.open("rb") as incoming, (staging / spec.name).open("xb") as output:
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            if regular_hash(staging / spec.name) != expected:
                raise ArtifactError("Local artifact changed while copying")
            _verify(staging / spec.name, spec)
            return _publish(staging, store, spec, "explicit local selection")
    except OSError as error:
        raise ArtifactError(f"Could not stage local artifact ({type(error).__name__})") from None


def _copy(incoming, output, checkpoint):
    while True:
        if checkpoint:
            checkpoint()
        chunk = incoming.read(1024 * 1024)
        if not chunk:
            return
        output.write(chunk)


def acquire(spec, root, candidates=(), status=None, *, checkpoint=None):
    """Return an immutable accepted file, preserving all candidate source files."""
    spec.validate()
    if urlsplit(spec.url).scheme != "https":
        raise ArtifactError("Artifact downloads require HTTPS")
    if checkpoint:
        checkpoint()
    try:
        _, remote_digest = head_metadata(spec.url) if spec.digest is None else (None, None)
        expected = spec.digest or remote_digest
        if expected is not None:
            for candidate in candidates:
                if checkpoint:
                    checkpoint()
                try:
                    with _staged(root, spec) as (staging, store):
                        candidate = Path(candidate)
                        if status:
                            status(f"Checking cached {spec.name}…", None)
                        before = regular_hash(candidate, checkpoint=checkpoint)
                        if status:
                            status(f"Copying cached {spec.name}…", None)
                        with candidate.open("rb") as incoming, (staging / spec.name).open("xb") as output:
                            _copy(incoming, output, checkpoint)
                            output.flush()
                            os.fsync(output.fileno())
                        if regular_hash(staging / spec.name, checkpoint=checkpoint) != before:
                            raise ArtifactError("Candidate changed while copying")
                        _verify(staging / spec.name, spec, digest=expected, checkpoint=checkpoint)
                        if status:
                            status(f"Publishing accepted {spec.name}…", None)
                        return _publish(staging, store, spec, expected.source, checkpoint)
                except (OSError, ArtifactError):
                    continue

        with _staged(root, spec) as (staging, store):
            if checkpoint:
                checkpoint()
            if status:
                status(f"Downloading {spec.name}…", 0)
            with requests.get(
                spec.url, stream=True, allow_redirects=True, timeout=NETWORK_TIMEOUT,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                require_response(response, spec.url)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ArtifactError("Encoded artifact responses are unsupported")
                size, checksum = response_metadata(response.headers)
                received = 0
                with (staging / spec.name).open("xb") as output:
                    for chunk in response.iter_content(chunk_size=100 * 1024):
                        if checkpoint:
                            checkpoint()
                        if not chunk:
                            continue
                        output.write(chunk)
                        received += len(chunk)
                        if status:
                            status(f"Downloading {spec.name}…", min(received / size, 1) if size else None)
                    output.flush()
                    os.fsync(output.fileno())
            if status:
                status(f"Verifying {spec.name}…", None)
            _verify(staging / spec.name, spec, size, checksum, checkpoint)
            evidence = spec.digest.source if spec.digest else "completed HTTPS transfer and format acceptance"
            if status:
                status(f"Publishing accepted {spec.name}…", None)
            return _publish(staging, store, spec, evidence, checkpoint)
    except requests.RequestException as error:
        raise ArtifactError(f"Transfer failed for {safe_url(spec.url)} ({type(error).__name__})") from None
    except OSError as error:
        raise ArtifactError(f"Could not stage artifact ({type(error).__name__})") from None


def installed(path, kind):
    """Check a managed generation without claiming new remote verification."""
    path = Path(path)
    concrete = path.resolve(strict=True)
    sha256 = concrete.parent.name
    if len(sha256) != 64 or concrete.parent.parent.parent.name != ".artifacts":
        raise ArtifactError("Path is not an installed artifact generation")
    spec = ArtifactSpec("local:" + str(concrete), concrete.name, kind)
    accepted = AcceptedArtifact(concrete, spec, sha256, concrete.stat().st_size, "installed local identity")
    accepted.check()
    return accepted
