import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from logos_desktop import desktop, product_icon


class TestProductIcon(unittest.TestCase):
    def test_resource_bounds_reject_truncated_payload_and_oversized_group(self):
        payload = b'public icon fixture'
        group = struct.pack('<HHHBBBBHHIH', 0, 1, 1, 32, 32, 0, 0, 1, 32, len(payload), 7)
        group_resource = SimpleNamespace(OffsetToData=10, Size=len(group))
        icon_resource = SimpleNamespace(OffsetToData=100, Size=len(payload))

        def resource(kind, entries):
            return SimpleNamespace(id=kind, directory=SimpleNamespace(entries=entries))

        def image_entry(identifier, data):
            language = SimpleNamespace(data=SimpleNamespace(struct=data))
            return resource(identifier, [language])

        image = SimpleNamespace(
            parse_data_directories=lambda **kwargs: None,
            DIRECTORY_ENTRY_RESOURCE=SimpleNamespace(entries=[
                resource(14, [image_entry(1, group_resource)]), resource(3, [image_entry(7, icon_resource)]),
            ]),
        )
        with patch.object(product_icon.file_transactions, 'read_owned', return_value=b'fixture PE'):
            with patch.object(product_icon.pefile, 'PE') as parser:
                parser.return_value.__enter__.return_value = image
                image.get_data = lambda offset, size: group if offset == 10 else payload
                result = product_icon.icon_bytes('/fixture/Logos.exe')
                self.assertEqual(result[22:], payload)
                self.assertEqual(struct.unpack_from('<I', result, 18)[0], 22)

                image.get_data = lambda offset, size: group if offset == 10 else payload[:-1]
                with self.assertRaisesRegex(ValueError, 'bounds'):
                    product_icon.icon_bytes('/fixture/Logos.exe')
                image.get_data = lambda offset, size: struct.pack('<HHH', 0, 1, 257)
                with self.assertRaisesRegex(ValueError, 'group'):
                    product_icon.icon_bytes('/fixture/Logos.exe')

    def test_product_entry_matches_logos_windows_without_changing_controller_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            icon = root / 'logos-icon.png'
            with patch.object(desktop, '_default', return_value=''):
                plan = desktop.preview(root / 'profile.json', ['/controller/logos-desktop'],
                                       product_icon=icon, data_home=root / 'data', config_home=root / 'config')
            product = next(change.after.decode() for change in plan.files if change.path.stem.endswith('-app'))
            controller = next(change.after.decode() for change in plan.files if not change.path.stem.endswith('-app'))
            self.assertIn(f'Icon={icon}\n', product)
            self.assertIn('StartupWMClass=logos.exe\n', product)
            self.assertNotIn('StartupWMClass=', controller)
            self.assertNotIn(str(icon), controller)

    def test_icon_source_outside_selected_prefix_is_refused(self):
        app = SimpleNamespace(conf=SimpleNamespace(logos_exe='/other/Logos.exe', wine_prefix='/selected/prefix'))
        with self.assertRaisesRegex(ValueError, 'escapes'):
            product_icon.ensure(app)
