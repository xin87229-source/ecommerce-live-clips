"""Release archive boundaries and identity, with synthetic inputs only."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

MODULE = Path(__file__).resolve().parents[1] / 'tools/build_candidate.py'


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        for name, value in {'VERSION': '0.1.0-rc.2\n', 'cli.py': 'print("local")\n',
                            'README.md': '# Test\n', 'private-note.txt': 'do not ship\n',
                            'release-files.txt': 'VERSION\ncli.py\nREADME.md\nrelease-files.txt\n'}.items():
            (self.source / name).write_text(value, encoding='utf-8')
        self.out = self.root / 'candidates'
        spec = importlib.util.spec_from_file_location('candidate_builder', MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_exact_allowlist_and_content_identity(self):
        first = self.module.build(self.source, self.out)
        with zipfile.ZipFile(first['archive']) as archive:
            names = {name.split('/', 1)[1] for name in archive.namelist()}
            self.assertEqual(names, {'VERSION', 'cli.py', 'README.md', 'release-files.txt',
                                    'SOURCE-MANIFEST.json', 'CANDIDATE.json', 'PRIVACY-SCAN.json'})
            manifest = json.loads(archive.read(first['id'] + '/SOURCE-MANIFEST.json'))
            self.assertEqual(len(manifest['files']), 4)
        with self.assertRaises(FileExistsError):
            self.module.build(self.source, self.out)
        (self.source / 'README.md').write_text('# Revised\n', encoding='utf-8')
        self.assertNotEqual(first['id'], self.module.build(self.source, self.out)['id'])

    def test_rejects_unsafe_scope_and_private_content_before_output(self):
        original = (self.source / 'release-files.txt').read_text('utf-8')
        for extra in ('../secret.txt', 'outputs/data.json', 'config.json', 'movie.mp4'):
            with self.subTest(extra=extra):
                (self.source / 'release-files.txt').write_text(original + extra + '\n', encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.module.build(self.source, self.out)
                self.assertFalse(self.out.exists())
        (self.source / 'release-files.txt').write_text(original, encoding='utf-8')
        private_path = 'C:' + '\\' + 'Users' + '\\' + 'Synthetic'
        (self.source / 'README.md').write_text(private_path, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '隐私'):
            self.module.build(self.source, self.out)
        self.assertFalse(self.out.exists())

    def test_license_metadata_only_reports_distributed_license(self):
        # A local LICENSE omitted from the allowlist must not license the archive.
        (self.source / 'LICENSE').write_text('Synthetic license fixture\n', encoding='utf-8')
        without = self.module.build(self.source, self.out)
        missing = json.loads((Path(without['folder']) / 'CANDIDATE.json').read_text('utf-8'))
        self.assertEqual(missing['license_decision'], 'pending_owner_choice')
        with (self.source / 'release-files.txt').open('a', encoding='utf-8') as stream:
            stream.write('LICENSE\n')
        result = self.module.build(self.source, self.out)
        with zipfile.ZipFile(result['archive']) as archive:
            metadata = json.loads(archive.read(result['id'] + '/CANDIDATE.json'))
            self.assertEqual(metadata['license_decision'], 'see_LICENSE')
            self.assertEqual(archive.read(result['id'] + '/LICENSE'), (self.source / 'LICENSE').read_bytes())
            self.assertFalse(metadata['public_release_authorized'])
            self.assertEqual(metadata['status'], 'LOCAL_CANDIDATE_NOT_PUBLISHED')

    def test_rejects_symlink_file(self):
        linked = self.source / 'README.md'
        linked.unlink()
        outside = self.root / 'outside.md'
        outside.write_text('outside', encoding='utf-8')
        try:
            linked.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable; other scope checks still run')
        with self.assertRaises(ValueError):
            self.module.build(self.source, self.out)

    def test_rejects_quoted_secret_keys_and_escaped_private_paths(self):
        secret_key = 'api' + '_key'
        secret = 'sk-' + 'SYNTHETICEXAMPLE0123456789'
        quoted_json = json.dumps({secret_key: secret})
        quoted_dict = repr({secret_key: secret})
        machine_path = json.dumps({'path': 'C:' + '\\' + 'Users' + '\\' + 'Synthetic'})
        for content in (quoted_json, quoted_dict, machine_path):
            with self.subTest(content=content):
                (self.source / 'README.md').write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '隐私'):
                    self.module.build(self.source, self.out)
                self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
