"""Copy integrity and refusal boundaries use actual independent filesystem bytes."""

import errno
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from logos_desktop import runtime_copies as copies


class CopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "copied"
        self.identity_patch = patch.object(copies.runtime_filesystem, "identity", side_effect=self.identity)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def identity(self, path):
        info = Path(path).stat()
        return {"canonical_path": str(path), "filesystem_uuid": "fixture", "filesystem_root": str(path),
                "device": info.st_dev, "inode": info.st_ino, "owner_uid": info.st_uid}

    def populate(self):
        (self.source / "drive_c").mkdir()
        (self.source / "drive_c/data.db").write_bytes(b"source database")
        (self.source / "dosdevices").mkdir()
        (self.source / "dosdevices/c:").symlink_to("../drive_c")
        (self.source / "dosdevices/z:").symlink_to("/")
        with open(os.fsencode(self.source) + b"/non-utf8-\xff", "wb") as output:
            output.write(b"byte safe")

    def test_full_copy_preserves_manifest_and_database_changes_do_not_touch_source(self):
        self.populate()
        before = copies.manifest(self.source)
        receipt = copies.copy(self.source, self.destination, reflink=False)
        self.assertEqual(receipt["methods"]["full"], 2)
        self.assertEqual(before, copies.manifest(self.destination))
        original = self.source / "drive_c/data.db"
        cloned = self.destination / "drive_c/data.db"
        self.assertNotEqual(original.stat().st_ino, cloned.stat().st_ino)
        cloned.write_bytes(b"trial database changed")
        self.assertEqual(copies.manifest(self.source), before)
        self.assertFalse(self.destination.with_name("copied.partial").exists())

    def test_reflink_unsupported_falls_back(self):
        self.populate()
        original = copies.fcntl.ioctl

        def unsupported(fd, operation, argument):
            if operation == copies.FICLONE:
                raise OSError(errno.EOPNOTSUPP, "No reflink")
            return original(fd, operation, argument)

        with patch.object(copies.fcntl, "ioctl", side_effect=unsupported):
            receipt = copies.copy(self.source, self.destination)
        self.assertEqual(receipt["methods"], {"reflink": 0, "full": 2})

    def test_file_replacement_before_copy_open_is_rejected_without_waiting_on_fifo(self):
        self.populate()
        original = copies.entries
        replaced = False

        def changing_entries(root, **kwargs):
            nonlocal replaced
            for item in original(root, **kwargs):
                if kwargs.get("observe") and item["kind"] == "file" and not replaced:
                    path = self.source / os.fsdecode(copies.base64.b64decode(item["path"]))
                    # Only replace during the copy pass, after the manifest has been staged.
                    if self.root.joinpath("copied.partial").exists():
                        replaced = True
                        path.unlink()
                        os.mkfifo(path)
                yield item

        with patch.object(copies, "entries", side_effect=changing_entries):
            with self.assertRaisesRegex(copies.CopyError, "Source changed"):
                copies.copy(self.source, self.destination)
        self.assertTrue(replaced)
        self.assertFalse(self.destination.exists())

    def test_hardlink_and_external_link_refused(self):
        (self.source / "file").write_text("content")
        os.link(self.source / "file", self.root / "outside")
        with self.assertRaisesRegex(copies.CopyError, "Hardlinked"):
            copies.copy(self.source, self.destination)
        (self.root / "outside").unlink()
        (self.source / "escape").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(copies.CopyError, "External"):
            copies.copy(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_source_mutation_during_copy_cannot_publish(self):
        self.populate()

        def mutate(progress):
            (self.source / "drive_c/data.db").write_text("external writer")

        with self.assertRaises(copies.CopyError):
            copies.copy(self.source, self.destination, status=mutate)
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.root.joinpath("copied.partial").is_dir())

    def test_staging_root_replacement_cannot_redirect_writes_into_unrelated_host_tree(self):
        self.populate()
        before = copies.manifest(self.source)
        canary = self.root / "host-tree"
        canary.mkdir()
        (canary / "keep").write_text("unrelated host data")
        canary_before = copies.manifest(canary)
        replaced = False

        def replace_staging(progress):
            nonlocal replaced
            if not replaced:
                replaced = True
                staging = self.root / "copied.partial"
                staging.rename(self.root / "displaced-staging")
                staging.symlink_to(canary, target_is_directory=True)

        with self.assertRaises((copies.CopyError, OSError)):
            copies.copy(self.source, self.destination, status=replace_staging)
        self.assertTrue(replaced)
        self.assertEqual(copies.manifest(self.source), before)
        self.assertEqual(copies.manifest(self.source).observation_sha256, before.observation_sha256)
        self.assertEqual(copies.manifest(canary), canary_before)
        self.assertEqual(copies.manifest(canary).observation_sha256, canary_before.observation_sha256)
        self.assertFalse(self.destination.exists())

    def test_fsync_and_rename_failures_preserve_source_without_reporting_ready(self):
        for phase in ("fsync", "rename"):
            with self.subTest(phase=phase):
                source = self.root / ("source-" + phase)
                source.mkdir()
                (source / "database").write_text("source state")
                before = copies.manifest(source)
                target = copies.os if phase == "fsync" else copies.paths
                method = "fsync" if phase == "fsync" else "rename_no_replace"
                with patch.object(target, method, side_effect=OSError(errno.EIO, "injected publication failure")):
                    with self.assertRaises(OSError):
                        copies.copy(source, self.root / ("copy-" + phase))
                self.assertEqual(copies.manifest(source), before)
                self.assertFalse(self.root.joinpath("copy-" + phase).exists())

    def test_supported_user_xattrs_survive_independent_copy(self):
        self.populate()
        os.setxattr(self.source / "drive_c/data.db", "user.fixture", b"\x00\xffmetadata")
        before = copies.manifest(self.source)
        copies.copy(self.source, self.destination)
        self.assertEqual(os.getxattr(self.destination / "drive_c/data.db", "user.fixture"), b"\x00\xffmetadata")
        self.assertEqual(copies.manifest(self.destination), before)

    def test_cancellation_before_publication_preserves_source(self):
        self.populate()
        before = copies.manifest(self.source)

        def progress(value):
            raise copies.Cancelled()

        with self.assertRaises(copies.Cancelled):
            copies.copy(self.source, self.destination, status=progress)
        self.assertEqual(copies.manifest(self.source), before)
        self.assertFalse(self.destination.exists())

    def test_enospc_during_write_never_publishes(self):
        self.populate()
        before = copies.manifest(self.source)
        with patch.object(copies.os, "write", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                copies.copy(self.source, self.destination, reflink=False)
        self.assertEqual(copies.manifest(self.source), before)
        self.assertFalse(self.destination.exists())

    def test_low_space_refuses_before_staging_even_when_reflink_would_be_available(self):
        self.populate()
        before = copies.manifest(self.source)
        facts = SimpleNamespace(f_bavail=1, f_frsize=4096, f_files=1000, f_favail=1000)
        original = copies.fcntl.ioctl
        clones = []

        def ioctl(descriptor, operation, argument):
            if operation == copies.FICLONE:
                clones.append(operation)
                return 0
            return original(descriptor, operation, argument)

        with (patch.object(copies.os, "statvfs", return_value=facts),
              patch.object(copies.fcntl, "ioctl", side_effect=ioctl)):
            with self.assertRaises(copies.CopyError) as failure:
                copies.copy(self.source, self.destination)
        self.assertEqual(failure.exception.reason, "insufficient-space")
        self.assertEqual(clones, [])
        self.assertFalse((self.root / "copied.partial").exists())
        self.assertFalse((self.root / "copied.manifest.partial").exists())
        self.assertEqual(copies.manifest(self.source), before)

    def test_nested_bind_mount_and_overlap_refused_before_copy(self):
        self.populate()
        with patch.object(copies, "_mount_paths", return_value=[str(self.source / "drive_c")]):
            with self.assertRaisesRegex(copies.CopyError, "mount"):
                copies.copy(self.source, self.destination)
        with self.assertRaisesRegex(copies.CopyError, "overlap"):
            copies.copy(self.source, self.source / "nested")

    def test_physical_destination_alias_is_refused_before_any_staging_write(self):
        self.populate()
        parent = self.root / "alias"
        parent.mkdir()

        def filesystem(path):
            result = self.identity(path)
            if Path(path) == parent:
                result["filesystem_root"] = str(self.source / "hidden-alias")
            return result

        before = copies.manifest(self.source)
        with patch.object(copies.runtime_filesystem, "identity", side_effect=filesystem):
            with self.assertRaisesRegex(copies.CopyError, "Physical copy paths overlap"):
                copies.copy(self.source, parent / "copy")
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(copies.manifest(self.source), before)

    def test_existing_destination_is_preserved(self):
        self.populate()
        self.destination.mkdir()
        (self.destination / "keep").write_text("later work")
        with self.assertRaises(OSError):
            copies.copy(self.source, self.destination)
        self.assertEqual((self.destination / "keep").read_text(), "later work")

    def test_privileged_mode_and_fifo_are_refused(self):
        file = self.source / "special"
        file.write_text("content")
        file.chmod(0o4755)
        with self.assertRaises(copies.CopyError):
            copies.manifest(self.source)
        file.unlink()
        os.mkfifo(file)
        with self.assertRaisesRegex(copies.CopyError, "Special"):
            copies.manifest(self.source)


if __name__ == "__main__":
    unittest.main()
