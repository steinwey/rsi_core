import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from rsi import ROOT
from rsi.core.common import digest, file_map
from rsi.skills.directory import INDEX_PATH, read_skill_directory, immutable, validate_paths, directory_schema
from rsi.skills.index import build_index, validate_index, load_index, sha
from rsi.skills.store import load_initial_skill, SkillStore, SkillProposalRejected


class IndexTests(unittest.TestCase):
    def files(self):
        files, _ = load_initial_skill()
        files['references/testing.md'] = '# Testing\nCheck observable behavior. 中文 🧪\n'
        files[INDEX_PATH] = build_index(files)
        return files

    def test_legacy_hash_and_read_semantics(self):
        files, metadata = load_initial_skill()
        self.assertEqual(metadata['version'], '2e97278ac34006f9')
        self.assertEqual(read_skill_directory(ROOT / 'skills/test-and-diagnose'), file_map(ROOT / 'skills/test-and-diagnose'))
        managed, source = load_initial_skill(directory_schema=1, bootstrap=True)
        self.assertEqual(source['source_version'], digest(files))
        self.assertNotEqual(digest(managed), digest(files))
        self.assertEqual(immutable(managed), immutable(files))
        self.assertEqual(managed['SKILL.md'], files['SKILL.md'])

    def test_deterministic_index_and_unknown_provenance(self):
        files = self.files()
        self.assertEqual(build_index(files), build_index(dict(reversed(list(files.items())))))
        entry = validate_index(files)['entries'][0]
        self.assertEqual(entry['provenance'], [{'evidence_kind': 'legacy_unknown'}])
        self.assertEqual(entry['char_count'], len(files[entry['path']]))
        self.assertEqual(entry['content_sha256'], sha(files[entry['path']]))

    def test_invalid_schema_and_fields(self):
        files = self.files()
        data = validate_index(files)
        mutations = [lambda d: d.update(schema_version=True),
                     lambda d: d.update(schema_version=2),
                     lambda d: d['entries'].append(copy.deepcopy(d['entries'][0])),
                     lambda d: d['entries'][0].update(content_sha256='0'*64),
                     lambda d: d['entries'][0].update(char_count=0),
                     lambda d: d['entries'][0].update(path='../bad.md'),
                     lambda d: d['entries'][0].update(keywords=[]),
                     lambda d: d['entries'][0].update(extra='unsupported'),
                     lambda d: d['entries'][0].update(provenance=[{'evidence_kind': 'PASS'}]),
                     lambda d: d.update(entries=[])]
        for mutation in mutations:
            value = copy.deepcopy(data)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_index(files | {INDEX_PATH: yaml.safe_dump(value)})

    def test_duplicate_id_distinct_paths(self):
        files = self.files()
        files['references/other.md'] = 'Other\n'
        data = load_index(build_index(files))
        data['entries'][1]['id'] = data['entries'][0]['id']
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            validate_index(files | {INDEX_PATH: yaml.safe_dump(data)})

    def test_yaml_duplicates_aliases_invalid_and_limits(self):
        for text in ('[', 'schema_version: 1\nschema_version: 1\nentries: []',
                     'schema_version: 1\nentries: &a [*a]', 'x'*1048577,
                     '!!python/object/apply:os.system [echo bad]', 'null',
                     'schema_version: 1\nentries: [{id: a, id: b}]'):
            with self.subTest(text=text[:70]), self.assertRaises(ValueError):
                load_index(text)

    def test_permission_paths(self):
        for paths in (['../x.md'], ['a\\b.md'], ['/x.md'], ['C:/x.md'], ['con.md'],
                      ['a./x.md'], ['references/A.md', 'references/a.md'],
                      ['References/a.md', 'references/b.md'], ['a', 'a/b.md']):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                validate_paths(paths)

    def test_yaml_is_exact_path_and_old_mode_ignores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'SKILL.md').write_text('Guide', encoding='utf-8')
            (root / 'other.yaml').write_text('a: b', encoding='utf-8')
            self.assertEqual(read_skill_directory(root), {'SKILL.md': 'Guide'})
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                read_skill_directory(root, directory_schema=1)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'source.md'
            target.write_text('Source', encoding='utf-8')
            skill = root / 'skill'
            skill.mkdir()
            try:
                (skill / 'SKILL.md').symlink_to(target)
            except OSError as exc:
                self.skipTest(f'Host cannot create symlinks: {exc}')
            for schema in (0, 1):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    read_skill_directory(skill, directory_schema=schema)

    def test_create_delete_rename_index_and_fixed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed, _ = load_initial_skill(directory_schema=1, bootstrap=True)
            store = SkillStore(tmp, seed, directory_schema=1)
            tools = store.tools_hash
            first = store.current()
            added, _ = store.propose({'references/one.md': '# First\n'})
            self.assertEqual(store.current(), first)
            store.record(added, True, {})
            renamed, _ = store.propose({'references/one.md': None, 'references/two.md': '# First\n'})
            store.record(renamed, True, {})
            files = read_skill_directory(renamed, directory_schema=1)
            self.assertEqual([e['path'] for e in validate_index(files)['entries']], ['references/two.md'])
            removed, _ = store.propose({'references/two.md': None})
            self.assertEqual(removed, first)
            self.assertEqual(store.tools_hash, tools)
            for changes in ({INDEX_PATH: 'anything'}, {'scripts/inspect_project.py': 'bad'}, {'../bad.md': 'bad'}, {'SKILL.md': None}):
                with self.assertRaises(SkillProposalRejected):
                    store.propose(changes)
            with self.assertRaises(SkillProposalRejected):
                store.propose({}, managed_changes={INDEX_PATH: 'bad'})
            with self.assertRaises(SkillProposalRejected):
                store.propose({}, expected_base_version=first.name)
            self.assertEqual(store.current(), renamed)

    def test_disabled_features_and_reasoningbank(self):
        self.assertEqual(directory_schema({}), 0)
        self.assertEqual(directory_schema({'skill_update_method': 'reasoningbank'}), 0)
        with self.assertRaises(ValueError):
            directory_schema({'skill_directory': {'schema': 1}, 'skill_update_method': 'reasoningbank'})
        for feature in ('skill_maintenance', 'reference_maintenance', 'skill_routing'):
            with self.assertRaisesRegex(ValueError, 'P0'):
                directory_schema({feature: {'enabled': True}})

    def test_bootstrap_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed, _ = load_initial_skill()
            store = SkillStore(tmp, seed)
            before = file_map(store.current())
            managed, meta = load_initial_skill(store.current(), directory_schema=1, bootstrap=True)
            self.assertEqual(file_map(store.current()), before)
            self.assertNotEqual(meta['version'], store.current().name)
            self.assertEqual(validate_index(managed)['entries'], [])

    def test_symlink_policy_without_os_privilege(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'SKILL.md').write_text('Guide')
            original = Path.is_symlink
            def linked(path):
                return path.name == 'SKILL.md' or original(path)
            with patch.object(Path, 'is_symlink', linked), self.assertRaises(ValueError):
                read_skill_directory(root, directory_schema=1)

    def test_crlf_unicode_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'references').mkdir()
            (root / 'SKILL.md').write_bytes(b'Guide\r\n')
            (root / 'references/test.md').write_bytes('中文 🧪\r\n'.encode())
            files = read_skill_directory(root, directory_schema=1)
            self.assertEqual(files['references/test.md'], '中文 🧪\n')
            (root / 'routing').mkdir()
            (root / INDEX_PATH).write_text(build_index(files), encoding='utf-8', newline='\n')
            loaded = read_skill_directory(root, directory_schema=1)
            validate_index(loaded)
            store = SkillStore(root / 'versions-store', loaded, directory_schema=1)
            self.assertEqual(read_skill_directory(store.current(), directory_schema=1), loaded)
