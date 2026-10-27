"""MSI table inspection and bounded attestation of embedded application headers."""

from array import array
from dataclasses import dataclass
import os
import re
import stat
import struct
import zlib


METADATA_LIMIT = 64 * 1024 * 1024
TEXT_LIMIT = 16 * 1024 * 1024
OBJECT_LIMIT = 524288
ROW_LIMIT = 131072
STRING_LIMIT = 131072
TABLE_LIMIT = 256
COLUMN_LIMIT = 4096
TABLE_COLUMN_LIMIT = 128
STREAM_LIMIT = 4096
DIRECTORY_LIMIT = 4096
DIRECTORY_DEPTH_LIMIT = 64
DIRECTORY_WORK_LIMIT = 16384
CABINET_LIMIT = 2 * 1024 * 1024 * 1024
COMPRESSED_LIMIT = 128 * 1024 * 1024
OUTPUT_LIMIT = 256 * 1024 * 1024
BLOCK_LIMIT = 8192
HEADER_LIMIT = 65536
CFB_FAT_LIMIT = 32 * 1024 * 1024
CFB_FAT_WORK_LIMIT = 1024 * 1024 * 1024
CFB_DIRECTORY_LIMIT = 4096
CFB_DEPTH_LIMIT = 64


def require(condition, message):
    if not condition:
        raise ValueError(message)


def preflight(source, checkpoint=None):
    """Bound olefile's eager FAT allocation and recursive directory construction.

    Inspect only allocation metadata here. Payload chains remain the existing
    sector reader's responsibility. The caller keeps this descriptor for olefile.
    """
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    identity = os.fstat(source.fileno())
    require(stat.S_ISREG(identity.st_mode), "MSI must be a regular file")
    require(identity.st_size <= CABINET_LIMIT + METADATA_LIMIT, "MSI exceeds the inspection size limit")
    source.seek(0)
    header = source.read(512)
    require(len(header) == 512 and header[:8] == bytes.fromhex("d0cf11e0a1b11ae1"), "Invalid CFB header")
    major, order, shift, mini_shift = struct.unpack_from("<4H", header, 26)
    require((major, shift) in ((3, 9), (4, 12)) and order == 0xfffe and mini_shift == 6,
            "Unsupported CFB sector format")
    sector_size = 1 << shift
    require(identity.st_size % sector_size == 0, "Truncated CFB sector")
    sector_count = identity.st_size // sector_size - 1
    directories, fat_count, first_directory, _, cutoff, _, mini_count, first_difat, difat_count = (
        struct.unpack_from("<9I", header, 40)
    )
    require(cutoff == 4096 and (major != 3 or directories == 0), "Invalid CFB allocation header")
    require(0 < fat_count <= sector_count and fat_count * sector_size <= CFB_FAT_LIMIT,
            "CFB FAT byte budget exceeded")
    # olefile 0.47 concatenates the accumulated array for every FAT sector.
    # Bound cumulative copying, not just the final array's byte size.
    require(sector_size * fat_count * (fat_count + 1) // 2 <= CFB_FAT_WORK_LIMIT,
            "CFB FAT constructor-work budget exceeded")
    require(mini_count * sector_size <= METADATA_LIMIT // 16 + sector_size,
            "Oversized MSI MiniFAT")
    words = sector_size // 4
    expected_difat = (max(0, fat_count - 109) + words - 2) // (words - 1)
    require(difat_count == expected_difat, "Invalid CFB DIFAT count")
    end, free = 0xfffffffe, 0xffffffff

    def sector(number):
        checkpoint()
        require(0 <= number < sector_count, "Invalid CFB sector reference")
        source.seek((number + 1) * sector_size)
        content = source.read(sector_size)
        require(len(content) == sector_size, "Truncated CFB sector")
        return content

    fat_sectors = []
    allocation_sectors = set()

    def add_fat(numbers):
        ended = False
        for number in numbers:
            checkpoint()
            if number == free:
                ended = True
                continue
            require(not ended, "CFB FAT catalog has references after padding")
            require(number < sector_count and number not in allocation_sectors,
                    "Invalid or repeated CFB FAT sector")
            require(len(fat_sectors) < fat_count, "CFB FAT count mismatch")
            allocation_sectors.add(number)
            fat_sectors.append(number)

    add_fat(struct.unpack_from("<109I", header, 76))
    current = first_difat
    for _ in range(difat_count):
        require(current not in allocation_sectors, "Cyclic or overlapping CFB DIFAT")
        allocation_sectors.add(current)
        values = struct.unpack(f"<{words}I", sector(current))
        add_fat(values[:-1])
        current = values[-1]
    require(current in (end, free) and len(fat_sectors) == fat_count, "Invalid CFB DIFAT termination")
    require(fat_count == (sector_count + words - 1) // words, "Excessive CFB FAT capacity")

    fat = array("I")
    for number in fat_sectors:
        fat.extend(value for (value,) in struct.iter_unpack("<I", sector(number)))
    require(len(fat) >= sector_count, "CFB FAT does not cover the file")
    directory = bytearray()
    seen = set()
    current = first_directory
    while current != end:
        checkpoint()
        require(len(directory) + sector_size <= CFB_DIRECTORY_LIMIT * 128,
                "CFB directory-entry budget exceeded")
        require(current < sector_count and current not in seen and current not in allocation_sectors,
                "Invalid or cyclic CFB directory chain")
        seen.add(current)
        directory.extend(sector(current))
        current = fat[current]
    require(directory and (major != 4 or len(seen) == directories), "Invalid CFB directory extent")
    require(directory[66] == 5, "Invalid CFB root entry")
    root_size = struct.unpack_from("<Q", directory, 120)[0]
    require(root_size <= METADATA_LIMIT, "Oversized MSI mini stream")

    # Include sibling edges in depth. olefile recurses through these as well as
    # storage children. Repeated references are rejected before its constructor.
    pending = [(0, 0)]
    visited = set()
    while pending:
        checkpoint()
        index, depth = pending.pop()
        require(index < len(directory) // 128 and index not in visited,
                "Invalid or repeated CFB directory reference")
        require(depth <= CFB_DEPTH_LIMIT, "CFB directory-depth budget exceeded")
        visited.add(index)
        offset = index * 128
        kind = directory[offset + 66]
        require(kind in ((5,) if index == 0 else (1, 2)), "Invalid CFB directory entry type")
        left, right, child = struct.unpack_from("<3I", directory, offset + 68)
        require(kind != 2 or child == free, "CFB stream has directory children")
        require(index != 0 or (left == free and right == free), "Invalid CFB root siblings")
        for reference in (left, right, child):
            if reference != free:
                pending.append((reference, depth + 1))
    source.seek(0)
    return identity


def summary_information(content, checkpoint=None):
    """Read only the scalar SummaryInformation fields used for MSI acceptance."""
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    require(48 <= len(content) <= HEADER_LIMIT, "Invalid MSI summary size")
    require(content[:2] == b"\xfe\xff" and struct.unpack_from("<I", content, 24)[0] == 1
            and content[28:44] == bytes.fromhex("e0859ff2f94f6810ab9108002b27b3d9"),
            "Invalid MSI summary property set")
    start = struct.unpack_from("<I", content, 44)[0]
    require(48 <= start <= len(content) - 8 and start % 4 == 0, "Invalid MSI summary section")
    size, count = struct.unpack_from("<II", content, start)
    require(8 <= size <= len(content) - start and count <= (size - 8) // 8,
            "Invalid MSI summary catalog")
    catalog_end = 8 + count * 8
    offsets = {}
    for index in range(count):
        checkpoint()
        identifier, offset = struct.unpack_from("<II", content, start + 8 + index * 8)
        require(identifier not in offsets and catalog_end <= offset <= size - 4 and offset % 4 == 0,
                "Invalid MSI summary property offset or ID")
        offsets[identifier] = offset
    ordered = sorted((offset, identifier) for identifier, offset in offsets.items())
    require(len({offset for offset, _ in ordered}) == count, "Overlapping MSI summary properties")
    values = {}
    for index, (offset, identifier) in enumerate(ordered):
        checkpoint()
        limit = ordered[index + 1][0] if index + 1 < count else size
        raw = content[start + offset:start + limit]
        kind = struct.unpack_from("<I", raw)[0]
        # Unknown values are opaque. Their counts must never drive a decoder.
        if identifier not in (1, 7, 15):
            continue
        if identifier == 1:
            require(kind == 2 and len(raw) >= 6, "Invalid MSI summary codepage")
            values[1] = struct.unpack_from("<H", raw, 4)[0]
        elif identifier == 15:
            require(kind == 3 and len(raw) >= 8, "Invalid MSI source flags")
            values[15] = struct.unpack_from("<i", raw, 4)[0]
            require(values[15] >= 0, "Invalid MSI source flags")
        else:
            require(kind in (30, 31) and len(raw) >= 8, "Unsupported MSI template scalar")
            length = struct.unpack_from("<I", raw, 4)[0]
            width = 2 if kind == 31 else 1
            require(0 < length <= (len(raw) - 8) // width, "Invalid MSI template extent")
            text = raw[8:8 + length * width]
            require(text.endswith(b"\0" * width), "Unterminated MSI template")
            # The platform/language contract is ASCII even for Unicode sets.
            values[7] = text[:-width].decode("utf-16le" if kind == 31 else "ascii")
            require("\0" not in values[7], "Invalid MSI template text")
    require(7 in values, "Missing MSI template")
    return values


def version(value):
    require(isinstance(value, str) and len(value) <= 64
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", value),
            "Expected a four-component application version")
    parts = tuple(map(int, value.split(".")))
    require(all(part <= 65535 for part in parts), "Application version is out of range")
    return parts


@dataclass(frozen=True)
class MsiExpectation:
    product: str
    release: str

    def validate(self):
        require(self.product == "Logos", "Unsupported MSI product role")
        version(self.release)


def stream_name(name):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"
    output = []
    for character in name.removeprefix("\u4840"):
        code = ord(character)
        if 0x3800 <= code < 0x4800:
            output.extend((alphabet[(code - 0x3800) & 63], alphabet[(code - 0x3800) >> 6]))
        elif 0x4800 <= code < 0x4840:
            output.append(alphabet[code - 0x4800])
        else:
            output.append(character)
    return "".join(output)


def rows(content, widths):
    row_size = sum(widths)
    require(row_size > 0 and len(content) % row_size == 0, "Invalid MSI table length")
    count = len(content) // row_size
    for row in range(count):
        offset = 0
        values = []
        for size in widths:
            start = offset * count + row * size
            values.append(int.from_bytes(content[start:start + size], "little"))
            offset += size
        yield values


class Database:
    """Validate schemas once and expose the few tables needed by the consumer."""

    def __init__(self, ole, names, checkpoint=None):
        self.ole = ole
        self.names = names
        self.checkpoint = checkpoint or (lambda: None)
        self.metadata_bytes = 0
        self.text_bytes = 0
        self.object_count = 0
        self.row_count = 0
        pool, data = self.read("_StringPool"), self.read("_StringData")
        require(len(pool) >= 4 and len(pool) % 4 == 0, "Invalid MSI string pool")
        self.width = 3 if int.from_bytes(pool[2:4], "little") & 0x8000 else 2
        self.codepage = int.from_bytes(pool[:2], "little") or 1252
        self.strings = [b""]
        cursor, offset = 4, 0
        while cursor < len(pool):
            self.checkpoint()
            length, references = struct.unpack_from("<HH", pool, cursor)
            cursor += 4
            if not length and references:
                require(cursor + 4 <= len(pool), "Truncated MSI long string")
                length = int.from_bytes(pool[cursor:cursor + 4], "little")
                cursor += 4
            require(offset + length <= len(data), "MSI string pool exceeds its data")
            require(len(self.strings) < STRING_LIMIT, "MSI string-pool object budget exceeded")
            self.retain_objects(2)
            self.strings.append(data[offset:offset + length] if references else None)
            offset += length
        require(offset == len(data), "MSI string data does not match its pool")
        self.decoded = [None] * len(self.strings)

        self.schemas = {}
        for (reference,) in self.iter_rows(self.read("_Tables"), [self.width]):
            table = self.text(reference)
            require(len(self.schemas) < TABLE_LIMIT, "MSI table budget exceeded")
            require(table not in self.schemas, "Invalid MSI table catalog")
            self.retain_objects(2)
            self.schemas[table] = {}
        require("Property" in self.schemas, "Invalid MSI table catalog")
        column_count = 0
        for table, number, name, kind in self.iter_rows(self.read("_Columns"), [self.width, 2, self.width, 2]):
            table, number, kind = self.text(table), number - 0x8000, kind - 0x8000
            label = self.text(name)
            require(table in self.schemas and number >= 1 and number not in self.schemas[table]
                    and kind & 0x100, "Invalid MSI column catalog")
            column_count += 1
            require(column_count <= COLUMN_LIMIT and len(self.schemas[table]) < TABLE_COLUMN_LIMIT,
                    "MSI column budget exceeded")
            self.retain_objects(4)
            self.schemas[table][number] = (label, kind)
        self.contents = {}
        for table, columns in self.schemas.items():
            self.checkpoint()
            require(columns and sorted(columns) == list(range(1, len(columns) + 1)),
                    "MSI columns are missing or unordered")
            require(len({label for label, _ in columns.values()}) == len(columns),
                    "Duplicate MSI column name")
            widths = self.widths(table)
            if table in names:
                content = self.read(table)
                self.contents[table] = content
                ordered = self.columns(table)
                for values in self.iter_rows(content, widths):
                    for value, (_, kind) in zip(values, ordered):
                        if self.is_string(kind) and value:
                            require(value < len(self.strings) and self.strings[value] is not None,
                                    "MSI row has an invalid string reference")

    @staticmethod
    def is_string(kind):
        return kind & 0x800 and kind & ~0x1000 != 0x900

    def columns(self, table):
        return [self.schemas[table][number] for number in sorted(self.schemas[table])]

    def widths(self, table):
        widths = []
        for _, kind in self.columns(table):
            length = kind & 255
            if self.is_string(kind):
                widths.append(self.width)
            elif kind & ~0x1000 == 0x900:
                widths.append(2)
            else:
                require(length in (2, 4), "Invalid MSI column type")
                widths.append(length)
        return widths

    def read(self, name):
        self.checkpoint()
        require(name in self.names, "Missing MSI metadata stream")
        size = self.ole.get_size(self.names[name])
        self.metadata_bytes += size
        require(self.metadata_bytes <= METADATA_LIMIT, "Oversized MSI metadata")
        with self.ole.openstream(self.names[name]) as source:
            return source.read()

    def text(self, reference):
        require(0 < reference < len(self.strings) and self.strings[reference],
                "Invalid MSI string reference")
        if self.decoded[reference] is None:
            # Charge at most four bytes per character before decoding. Repeated
            # references reuse this one string and do not spend this budget again.
            raw = self.strings[reference]
            require(len(raw) * 4 <= TEXT_LIMIT - self.text_bytes, "MSI expanded-text budget exceeded")
            encoding = "utf-8" if self.codepage == 65001 else f"cp{self.codepage}"
            self.decoded[reference] = self.retain_text(raw.decode(encoding))
        return self.decoded[reference]

    def retain_objects(self, count):
        self.object_count += count
        require(self.object_count <= OBJECT_LIMIT, "MSI metadata object budget exceeded")

    def retain_text(self, text):
        self.text_bytes += len(text) * 4
        require(self.text_bytes <= TEXT_LIMIT, "MSI expanded-text budget exceeded")
        self.retain_objects(1)
        return text

    def iter_rows(self, content, widths):
        for values in rows(content, widths):
            self.checkpoint()
            self.row_count += 1
            require(self.row_count <= ROW_LIMIT, "MSI row budget exceeded")
            yield values

    def table(self, name, fields):
        require(name in self.contents, f"Missing MSI {name} table")
        columns = self.columns(name)
        widths = self.widths(name)
        require(set(fields).issubset(label for label, _ in columns), f"Invalid MSI {name} schema")
        selected = [(index, label, kind, width)
                    for index, ((label, kind), width) in enumerate(zip(columns, widths)) if label in fields]
        for values in self.iter_rows(self.contents[name], widths):
            # Include dictionary/index entries, field slots and numeric values.
            # Charging every yielded row also bounds repeat consumption of a table.
            self.retain_objects(2 + 2 * len(selected))
            record = {}
            for index, label, kind, width in selected:
                value = values[index]
                if not value:
                    record[label] = None
                elif self.is_string(kind):
                    record[label] = self.text(value)
                else:
                    record[label] = value - (1 << (width * 8 - 1))
            yield record


def keyed(records, key):
    result = {}
    for record in records:
        value = record[key]
        require(value is not None and value not in result, f"Duplicate or missing MSI {key}")
        result[value] = record
    return result


def target_names(database, value, *, directory=False):
    """Compare a conservative Win32 target-name subset, not MSI identifiers."""
    require(isinstance(value, str) and 0 < len(value) <= 1023, "Invalid MSI target name")
    paths = value.split(":") if directory else [value]
    require(len(paths) <= 2 and all(paths), "Invalid MSI target/source directory")
    targets = []
    for index, path in enumerate(paths):
        variants = path.split("|")
        require(len(variants) <= 2, "Invalid MSI short/long target name")
        for name in variants:
            if directory and name == "." and len(variants) == 1:
                continue
            require(0 < len(name) <= 255 and name not in (".", "..") and name[-1] not in (".", " ")
                    and not any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name),
                    "Invalid or aliased MSI target segment")
            if index == 0:
                folded = name.casefold()
                require(len(folded) <= 255, "Unsupported MSI target-name expansion")
                database.retain_objects(1)
                targets.append(database.retain_text(folded))
    return tuple(targets)


def application_directories(database, product):
    """Resolve static target nodes once, collapsing target-dot aliases.

    Standard folder properties are distinct anchors, even when their DefaultDir
    is a dot. Other top-level folder properties and runtime directory changes
    are outside this static layout contract.
    """
    directories = keyed(database.table("Directory", ("Directory", "Directory_Parent", "DefaultDir")), "Directory")
    require(len(directories) <= DIRECTORY_LIMIT, "MSI directory-node budget exceeded")
    anchors = {"TARGETDIR": 0, "LocalAppDataFolder": 1, "DesktopFolder": 2, "ProgramMenuFolder": 3}
    require("TARGETDIR" in directories and "LocalAppDataFolder" in directories,
            "Missing MSI application directory anchors")
    names = {}
    work = 0

    def step():
        nonlocal work
        database.checkpoint()
        work += 1
        require(work <= DIRECTORY_WORK_LIMIT, "MSI directory-work budget exceeded")

    for identifier, directory in directories.items():
        step()
        require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,71}", identifier),
                "Invalid MSI directory identifier")
        parent = directory["Directory_Parent"]
        aliases = target_names(database, directory["DefaultDir"], directory=True)
        if identifier == "TARGETDIR":
            require(parent is None and aliases[-1:] == ("sourcedir",), "Invalid MSI target-directory anchor")
        else:
            require(isinstance(parent, str) and parent in directories, "Missing MSI directory parent")
            if identifier in anchors:
                require(parent == "TARGETDIR" and not aliases, "Unsupported MSI standard-folder layout")
            else:
                require(parent != "TARGETDIR", "Unsupported MSI top-level directory property")
        database.retain_objects(8 + 4 * len(aliases))
        names[identifier] = aliases

    targets = {identifier: node for identifier, node in anchors.items() if identifier in directories}
    depths = {identifier: 0 if identifier == "TARGETDIR" else 1 for identifier in targets}
    namespace = {}
    next_node = len(anchors)
    for identifier in directories:
        if identifier in targets:
            continue
        trail = []
        visiting = set()
        current = identifier
        while current not in targets:
            step()
            require(current not in visiting, "Cyclic MSI directory parents")
            visiting.add(current)
            trail.append(current)
            require(len(trail) <= DIRECTORY_DEPTH_LIMIT, "MSI directory-depth budget exceeded")
            current = directories[current]["Directory_Parent"]

        for current in reversed(trail):
            step()
            parent = directories[current]["Directory_Parent"]
            depth = depths[parent] + 1
            require(depth <= DIRECTORY_DEPTH_LIMIT, "MSI directory-depth budget exceeded")
            parent_node = targets[parent]
            aliases = names[current]
            if aliases:
                for alias in aliases:
                    require((parent_node, alias) not in namespace,
                            "Case-equivalent MSI application directories")
                node = next_node
                next_node += 1
                for alias in aliases:
                    namespace[parent_node, alias] = node
            else:
                node = parent_node
            targets[current] = node
            depths[current] = depth

    roots = set()
    for identifier, aliases in names.items():
        step()
        if identifier not in anchors and aliases and aliases[-1] == product.casefold():
            parent = directories[identifier]["Directory_Parent"]
            if targets[parent] == targets["LocalAppDataFolder"]:
                roots.add(targets[identifier])
    require(len(roots) == 1, "MSI application root is missing or ambiguous")
    return targets, roots.pop()


def primary_file(database, expectation):
    expectation.validate()
    properties = keyed(database.table("Property", ("Property", "Value")), "Property")
    name = properties.get("ProductName", {}).get("Value")
    require(isinstance(name, str) and re.match(rf"{expectation.product}(?:\s|$)", name),
            "MSI product does not match the requested role")
    product_version = properties.get("ProductVersion", {}).get("Value")
    require(isinstance(product_version, str)
            and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,5}", product_version),
            "Invalid MSI ProductVersion")
    major, minor, build = map(int, product_version.split("."))
    require(major <= 255 and minor <= 255 and build <= 65535, "Invalid MSI ProductVersion")

    directory_targets, root = application_directories(database, expectation.product)
    components = keyed(database.table("Component", ("Component", "Directory_")), "Component")
    for component in components.values():
        database.checkpoint()
        require(component["Directory_"] in directory_targets, "Missing MSI component target directory")
    fields = ("File", "Component_", "FileName", "FileSize", "Version", "Attributes", "Sequence")
    files = keyed(database.table("File", fields), "File")
    candidates = []
    file_targets = set()
    for file in files.values():
        database.checkpoint()
        component = components.get(file["Component_"])
        require(component is not None, "MSI file has no component")
        if directory_targets[component["Directory_"]] == root:
            aliases = target_names(database, file["FileName"])
            require(file_targets.isdisjoint(aliases), "Case-equivalent MSI application files")
            file_targets.update(aliases)
            if aliases[-1] == f"{expectation.product}.exe".casefold():
                candidates.append(file)
    require(len(candidates) == 1, "MSI primary executable is missing or ambiguous")
    primary = candidates[0]
    # MSI ProductVersion has three fields. The four-field File Version binds the
    # requested vendor release without guessing how its build number was packed.
    require(version(primary["Version"]) == version(expectation.release), "MSI application version mismatch")
    require(isinstance(primary["FileSize"], int) and primary["FileSize"] >= 64,
            "Invalid MSI primary executable size")
    return primary


class SectorReader:
    """Bounded regular-stream reads using olefile 0.47's directory and FAT layout.

    openstream constructs the whole stream in memory. Keep this adapter limited
    to regular sectors and verify the complete chain before reading any payload.
    """

    def __init__(self, ole, parts, checkpoint=None):
        self.ole = ole
        self.checkpoint = checkpoint or (lambda: None)
        entry = ole.direntries[ole._find(parts)]
        self.size = entry.size
        require(ole.minisectorcutoff <= self.size <= CABINET_LIMIT, "Unsupported MSI stream size")
        count = (self.size + ole.sectorsize - 1) // ole.sectorsize
        require(count <= len(ole.fat) and ole._filesize <= CABINET_LIMIT + METADATA_LIMIT,
                "Oversized MSI sector chain")
        self.chain = array("I")
        seen = bytearray((len(ole.fat) + 7) // 8)
        sector = entry.isectStart
        for _ in range(count):
            self.checkpoint()
            require(0 <= sector < len(ole.fat) and (sector + 2) * ole.sectorsize <= ole._filesize,
                    "Invalid MSI stream sector")
            index, bit = divmod(sector, 8)
            require(not seen[index] & (1 << bit), "Cyclic MSI stream sector chain")
            seen[index] |= 1 << bit
            self.chain.append(sector)
            sector = int(ole.fat[sector])
        require(sector == 0xfffffffe, "Invalid MSI stream chain termination")

    def read(self, offset, length):
        require(0 <= length <= HEADER_LIMIT and 0 <= offset <= self.size
                and offset + length <= self.size, "MSI stream read out of bounds")
        output = bytearray()
        while length:
            self.checkpoint()
            index, within = divmod(offset, self.ole.sectorsize)
            block = self.ole.getsect(self.chain[index])
            count = min(length, self.ole.sectorsize - within)
            output.extend(block[within:within + count])
            offset += count
            length -= count
        return bytes(output)


def checksum(data):
    value = 0
    end = len(data) - len(data) % 4
    for (word,) in struct.iter_unpack("<I", data[:end]):
        value ^= word
    # CAB's remainder bytes use big-endian order, unlike its complete words.
    return value ^ int.from_bytes(data[end:], "big")


def cabinet_header(reader, file):
    header = reader.read(0, min(reader.size, HEADER_LIMIT))
    require(len(header) >= 36 and header[:4] == b"MSCF", "Invalid embedded CAB header")
    require(all(struct.unpack_from("<I", header, offset)[0] == 0 for offset in (4, 12, 20)),
            "Invalid CAB reserved header fields")
    size, file_offset = struct.unpack_from("<I4xI", header, 8)
    minor, major, folder_count, file_count, flags, _, index = struct.unpack_from("<BB5H", header, 24)
    require(size == reader.size and (major, minor) == (1, 3), "Invalid embedded CAB extent or version")
    require(flags == 0 and index == 0, "Unsupported reserved or spanned CAB layout")
    folder_end = 36 + folder_count * 8
    require(folder_count > 0 and folder_end <= file_offset < len(header), "Invalid CAB folder catalog")
    folders = [struct.unpack_from("<IHH", header, 36 + 8 * number) for number in range(folder_count)]
    cursor = file_offset
    selected = []
    names = set()
    ranges = {}
    for _ in range(file_count):
        reader.checkpoint()
        require(cursor + 16 < len(header), "CAB file catalog exceeds inspection budget")
        length, offset, folder, _, _, attributes = struct.unpack_from("<II4H", header, cursor)
        cursor += 16
        end = header.find(b"\0", cursor)
        require(end >= cursor and end - cursor <= 255, "Invalid CAB member name")
        name = header[cursor:end].decode("utf-8" if attributes & 0x80 else "ascii")
        cursor = end + 1
        require(name and name not in names and "/" not in name and "\\" not in name
                and ":" not in name and name not in (".", ".."), "Unsafe or duplicate CAB member")
        names.add(name)
        require(folder < folder_count and offset + length <= CABINET_LIMIT,
                "Invalid or continued CAB member")
        ranges.setdefault(folder, set()).add((offset, offset + length))
        if name == file["File"]:
            require(length == file["FileSize"], "CAB primary executable size mismatch")
            selected.append((folder, offset, length))
    require(len(selected) == 1, "CAB primary executable is missing or ambiguous")
    # Vendor cabinets can alias identical content under distinct File keys.
    # Exact ranges are safe to share, partial overlaps are not.
    for intervals in ranges.values():
        ordered = sorted(intervals)
        require(all(previous[1] <= current[0] for previous, current in zip(ordered, ordered[1:])),
                "Partially overlapping CAB members")
    for start, blocks, compression in folders:
        require(cursor <= start < size and blocks > 0, "Invalid CAB folder extent")
        require(compression == 1, "Unsupported CAB compression, expected MSZIP")
    folder, offset, length = selected[0]
    start, blocks, compression = folders[folder]
    limit = min((position for position, _, _ in folders if position > start), default=size)
    require(len({position for position, _, _ in folders}) == len(folders), "Overlapping CAB folders")
    # Inspect declared block extents without inflating the rest of this folder.
    # The PE prefix must belong to a complete member, not a truncated file claim.
    position, total = start, 0
    for _ in range(blocks):
        reader.checkpoint()
        require(position + 8 <= limit, "Truncated CAB folder header")
        _, packed, unpacked = struct.unpack("<IHH", reader.read(position, 8))
        require(2 < packed <= 38912 and 0 < unpacked <= 32768, "Invalid MSZIP block length")
        position += 8 + packed
        total += unpacked
        require(position <= limit, "CAB data exceeds its folder")
    require(max(end for _, end in ranges[folder]) <= total, "CAB member exceeds uncompressed folder extent")
    return start, limit, blocks, offset, length


def attest_cabinet(reader, file):
    start, folder_end, blocks, target, length = cabinet_header(reader, file)
    wanted = min(length, HEADER_LIMIT)
    require(target + wanted <= OUTPUT_LIMIT, "CAB attestation output budget exceeded")
    cursor, decoded, compressed = start, 0, 0
    dictionary = b""
    prefix = bytearray()
    for _ in range(min(blocks, BLOCK_LIMIT)):
        reader.checkpoint()
        require(cursor + 8 <= folder_end, "Truncated CAB data header")
        raw_header = reader.read(cursor, 8)
        supplied, packed, unpacked = struct.unpack("<IHH", raw_header)
        require(2 < packed <= 38912 and 0 < unpacked <= 32768, "Invalid MSZIP block length")
        compressed += packed
        require(compressed <= COMPRESSED_LIMIT and decoded + unpacked <= OUTPUT_LIMIT,
                "CAB attestation byte budget exceeded")
        require(cursor + 8 + packed <= folder_end, "CAB data exceeds its folder")
        content = reader.read(cursor + 8, packed)
        require(not supplied or supplied == checksum(content) ^ checksum(raw_header[4:]),
                "CAB data checksum mismatch")
        require(content[:2] == b"CK", "Invalid MSZIP block signature")
        decoder = zlib.decompressobj(-15, zdict=dictionary) if dictionary else zlib.decompressobj(-15)
        output = decoder.decompress(content[2:], unpacked + 1)
        require(len(output) == unpacked and decoder.eof and not decoder.unused_data
                and not decoder.unconsumed_tail, "Invalid MSZIP compressed block")
        dictionary = (dictionary + output)[-32768:]
        first, last = max(decoded, target), min(decoded + unpacked, target + wanted)
        if last > first:
            prefix.extend(output[first - decoded:last - decoded])
        decoded += unpacked
        cursor += 8 + packed
        if len(prefix) == wanted:
            break
    require(len(prefix) == wanted, "CAB attestation block budget exceeded or file truncated")
    require(prefix[:2] == b"MZ", "MSI primary executable is not a PE image")
    pe = struct.unpack_from("<I", prefix, 60)[0]
    require(64 <= pe <= len(prefix) - 26 and prefix[pe:pe + 4] == b"PE\0\0",
            "Invalid MSI primary PE header")
    machine = struct.unpack_from("<H", prefix, pe + 4)[0]
    optional_size = struct.unpack_from("<H", prefix, pe + 20)[0]
    require(machine == 0x8664 and optional_size >= 112 and pe + 24 + optional_size <= length
            and struct.unpack_from("<H", prefix, pe + 24)[0] == 0x20b,
            "Expected an AMD64 PE32+ primary executable")


def attest(database, file, summary_flags):
    sequence, attributes = file["Sequence"], file["Attributes"] or 0
    require(isinstance(sequence, int) and sequence > 0, "Invalid MSI file sequence")
    require(isinstance(attributes, int) and 0 <= attributes <= 32767, "Invalid MSI file attributes")
    require(not attributes & 8192 and (attributes & 16384 or summary_flags & 2),
            "MSI primary executable is not a compressed source")
    media = keyed(database.table("Media", ("DiskId", "LastSequence", "Cabinet")), "DiskId")
    require(all(isinstance(identifier, int) and identifier > 0 for identifier in media),
            "Invalid MSI media disk identifier")
    previous = 0
    chosen = None
    for row in sorted(media.values(), key=lambda row: row["DiskId"]):
        require(isinstance(row["LastSequence"], int) and row["LastSequence"] > previous,
                "Invalid MSI media sequence")
        if previous < sequence <= row["LastSequence"]:
            chosen = row
        previous = row["LastSequence"]
    cabinet = chosen["Cabinet"] if chosen else None
    require(isinstance(cabinet, str) and cabinet.startswith("#") and cabinet[1:] in database.names,
            "Expected an embedded primary MSI cabinet")
    reader = SectorReader(database.ole, database.names[cabinet[1:]], database.checkpoint)
    attest_cabinet(reader, file)
