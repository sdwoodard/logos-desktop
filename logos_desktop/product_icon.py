"""Extract locally installed Logos artwork without redistributing vendor assets."""

import logging
from pathlib import Path
import struct

import pefile

from . import config_store, file_transactions


def icon_bytes(executable):
    """Rebuild a bounded ICO from the executable's first icon resource group."""
    content = file_transactions.read_owned(executable, limit=64 * 1024 * 1024)
    with pefile.PE(data=content, fast_load=True) as image:
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        resources = {entry.id: entry.directory.entries for entry in image.DIRECTORY_ENTRY_RESOURCE.entries}
        group = resources[14][0].directory.entries[0].data.struct
        data = image.get_data(group.OffsetToData, group.Size)
        reserved, kind, count = struct.unpack_from('<HHH', data)
        if reserved != 0 or kind != 1 or not 0 < count <= 256 or len(data) != 6 + 14 * count:
            raise ValueError('Invalid icon resource group')
        icons = {entry.id: entry.directory.entries[0].data.struct for entry in resources[3]}
        directory = bytearray(data[:6])
        pixels = bytearray()
        for index in range(count):
            entry = data[6 + 14 * index:20 + 14 * index]
            size, resource_id = struct.unpack_from('<IH', entry, 8)
            resource = icons[resource_id]
            if size != resource.Size or not 0 < size <= 4 * 1024 * 1024:
                raise ValueError('Invalid icon image size')
            payload = image.get_data(resource.OffsetToData, size)
            if len(payload) != size or len(pixels) + size > 4 * 1024 * 1024:
                raise ValueError('Icon image exceeds its bounds')
            directory.extend(entry[:12] + struct.pack('<I', 6 + 16 * count + len(pixels)))
            pixels.extend(payload)
        return bytes(directory + pixels)


def ensure(app):
    """Publish a PNG alongside the selected installation's runtime data."""
    executable = getattr(app.conf, 'logos_exe', None)
    if not isinstance(executable, str):
        return None
    prefix = Path(app.conf.wine_prefix).resolve()
    if not Path(executable).resolve().is_relative_to(prefix):
        raise ValueError('Product icon source escapes the selected prefix')
    target = prefix.parent / 'logos-icon.png'
    try:
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QImage

        existing = file_transactions.read_owned(target, limit=4 * 1024 * 1024)
        if existing and not QImage.fromData(existing, 'PNG').isNull():
            return target
        image = QImage.fromData(icon_bytes(executable), 'ICO')
        if image.isNull() or image.width() > 1024 or image.height() > 1024:
            raise ValueError('Product icon could not be decoded')
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):
            raise ValueError('Product icon could not be encoded')
        with config_store.Target(target) as publication:
            publication.stage(bytes(encoded))
            publication.publish()
        return target
    except (ImportError, OSError, ValueError, KeyError, AttributeError, struct.error, pefile.PEFormatError) as error:
        logging.warning('Could not extract the installed Logos icon: %s', error)
        return None
