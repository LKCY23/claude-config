"""Single-read snapshots and batched pinned resource reads keep drift detection."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core import catalog
from claude_config.core.catalog import CatalogResolver
from claude_config.core.native import parse_native, render_native, snapshot_read
from claude_config.core.transaction import PlanStale
from test_profile_resources import ResourceFixture


class NativeReadTests(unittest.TestCase):
    def test_snapshot_bytes_hash_and_mode_describe_same_read(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config'
            content = b'actual native bytes\n'
            path.write_bytes(content)
            path.chmod(0o640)
            self.assertEqual(snapshot_read(path), (content, {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o640}))
            self.assertEqual(snapshot_read(path.parent / 'missing'), (b'', {'exists': False}))

    def test_replacement_or_removal_during_read_is_not_accepted(self):
        for replacement in (True, False):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'config'
                path.write_bytes(b'original')
                alternate = path.parent / 'replacement'
                alternate.write_bytes(b'original')
                original = os.fstat
                calls = 0
                def fstat(descriptor):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        os.replace(alternate, path) if replacement else path.unlink()
                    return original(descriptor)
                with patch('claude_config.core.native.os.fstat', side_effect=fstat), self.assertRaises(PlanStale):
                    snapshot_read(path)

    def test_edit_during_read_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config'
            path.write_bytes(b'original')
            original = os.fstat
            calls = 0
            def fstat(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    path.write_bytes(b'changed')
                return original(descriptor)
            with patch('claude_config.core.native.os.fstat', side_effect=fstat), self.assertRaises(PlanStale):
                snapshot_read(path)

    def test_non_regular_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'not-a-config'
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                snapshot_read(path)

    def test_read_toml_without_format_parser_and_preserve_comments_on_write(self):
        content = b'# preserve comment\nmodel = "old"\n[desktop]\n"local.key" = "keep"\n'
        with patch('claude_config.core.native.tomlkit.parse', side_effect=AssertionError('read should not construct formatting tree')):
            value = parse_native('codex', content)
        wanted = deepcopy(value)
        wanted['model'] = 'new'
        updated = render_native('codex', content, wanted)
        self.assertEqual(parse_native('codex', updated), wanted)
        self.assertIn(b'# preserve comment', updated)
        self.assertIn(b'"local.key" = "keep"', updated)
        self.assertEqual(render_native('codex', content, value), content)


class BatchedCatalogTests(ResourceFixture):
    def test_many_package_files_use_one_blob_request_and_one_index_request(self):
        files = {'SKILL.md': '---\nname: small\ndescription: test\n---\n'}
        files.update({f'references/file {i}.txt': f'body {i}' for i in range(40)})
        self.add('small', 'skill', files, 'SKILL.md')
        self.save()
        with patch('claude_config.core.catalog.git', wraps=catalog.git) as calls:
            packages = CatalogResolver(self.manager()).resolve(self.profile)
        self.assertEqual(packages['small@test'].files, {name: body.encode() for name, body in files.items()})
        blob_calls = [call for call in calls.call_args_list if 'cat-file' in call.args]
        index_calls = [call for call in calls.call_args_list if 'ls-files' in call.args]
        self.assertEqual(len(blob_calls), 1)
        self.assertIn('--batch', blob_calls[0].args)
        self.assertEqual(len(index_calls), 1)


if __name__ == '__main__':
    unittest.main()
