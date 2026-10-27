"""Bounded owned-file reads and journaled desktop-file publication and recovery."""

from base64 import b64decode, b64encode
from contextlib import ExitStack
import os
import stat

from . import config_store, runtime


MAX_CONFIG_BYTES = 1024 * 1024


class FileTransactionError(config_store.ConfigurationError):
    """A file operation needs a new preview or recovery before continuing."""


def read_owned(path, *, limit=MAX_CONFIG_BYTES):
    """Read an existing owned file without creating directories or following links."""
    path = config_store.absolute_path(path)
    runtime._safe_target(path)
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise FileTransactionError(f"Expected an owned regular file: {path}")
            content = source.read(limit + 1)
            if len(content) > limit:
                raise FileTransactionError(f"File exceeds the inspection limit: {path}")
            return content
    finally:
        os.close(parent)


def encode_content(content):
    return None if content is None else b64encode(content).decode('ascii')


def decode_content(content):
    return None if content is None else b64decode(content, validate=True)


def write_record(path, record):
    content = config_store.encode(record)
    if len(content) > 8 * MAX_CONFIG_BYTES:
        raise FileTransactionError('Recovery journal exceeds the publication limit')
    with config_store.Target(path) as target:
        target.stage(content)
        target.publish()
        os.fsync(target.fd)
        if read_owned(path, limit=8 * MAX_CONFIG_BYTES) != content:
            raise FileTransactionError(f'Recovery journal readback changed: {path}')


def publish_files(journal, record):
    """Write intent durably before any publication, including absent-file states."""
    with ExitStack() as stack:
        targets = [stack.enter_context(config_store.Target(item['path'])) for item in record['files']]
        for target, item in zip(targets, record['files']):
            if target.previous != decode_content(item['before']):
                raise FileTransactionError(f'Target changed since preview: {target.path}')
            if 'before_mode' in item:
                mode = (stat.S_IMODE(os.stat(target.path.name, dir_fd=target.fd, follow_symlinks=False).st_mode)
                        if target.previous is not None else None)
                if mode != item['before_mode']:
                    raise FileTransactionError(f'Target permissions changed since preview: {target.path}')
            if 'mode' in item:
                target.stage(decode_content(item['after']), mode=item['mode'])
            else:
                target.stage(decode_content(item['after']))
        write_record(journal, record)

        for target, item in zip(targets, record['files']):
            target.publish()
            os.fsync(target.fd)
            if read_owned(target.path) != decode_content(item['after']):
                raise FileTransactionError(f'Publication readback changed: {target.path}')
        record['state'] = 'committed'
        write_record(journal, record)


def rollback_files(journal, record, *, preserve=(), dependents=None):
    dependents = dependents or {}
    conflicts = []

    def retain(path):
        for name in (path, *dependents.get(path, ())):
            if name not in conflicts:
                conflicts.append(name)

    for path in preserve:
        retain(path)
    for item in reversed(record['files']):
        if item['path'] in conflicts:
            continue
        with config_store.Target(item['path']) as target:
            before, after = decode_content(item['before']), decode_content(item['after'])
            current_mode = (stat.S_IMODE(os.stat(target.path.name, dir_fd=target.fd,
                                                follow_symlinks=False).st_mode)
                            if target.previous is not None else None)
            if target.previous == before and ('before_mode' not in item or current_mode == item['before_mode']):
                continue
            identity = item.get('publication_identity')
            same_publication = identity is None or (
                target.identity is not None and list(target.identity[:2]) == identity
            )
            if (target.previous != after or not same_publication
                    or ('mode' in item and current_mode != item['mode'])):
                retain(str(target.path))
                continue
            target.validate()
            if before is None:
                os.unlink(target.path.name, dir_fd=target.fd)
            else:
                if 'before_mode' in item:
                    target.stage(before, mode=item['before_mode'])
                else:
                    target.stage(before)
                target.publish()
            os.fsync(target.fd)

    record['state'] = 'conflict' if conflicts else 'rolled-back'
    record['conflicts'] = conflicts
    write_record(journal, record)
    return tuple(conflicts)
