import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from solvewright.round_pack import pack_round
from solvewright.worker import dispatch


class RoundPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / '.examiner' / 'pack'
        self.root.mkdir(parents=True)
        self.state = {'id': '11111111-1111-4111-8111-111111111111', 'finished': True, 'busy': False,
                      'title': 'Switches', 'parts': 2, 'settings': {'language': 'Python', 'style': 'tech',
                      'difficulty': 'foundation', 'minutes': 15}, 'messages': [{'text': 'PRIVATE CONVERSATION'}]}
        self.manifest = {'format': 1, 'summary': 'Control a switch.', 'files': {'solution.py': 'starter.py'}, 'parts': [], 'debrief': 'debrief.txt'}
        for name, content in {'starter.py': '# starter', 'debrief.txt': 'What a strong design handles.'}.items():
            (self.root / name).write_text(content)
        for number in (1, 2):
            for name in ('brief', 'rules', 'public', 'private', 'solution'):
                (self.root / f'{name}{number}.txt').write_text(f'{name} at part {number}')
            self.manifest['parts'].append({'number': number, 'title': f'Step {number}', 'minutes': 5,
                'brief': f'brief{number}.txt', 'rules': f'rules{number}.txt', 'solution': f'solution{number}.txt',
                'tests': {'path': f'tests/test_part{number}.py', 'source': f'public{number}.txt'},
                'checks': {'path': f'test_checks{number}.py', 'source': f'private{number}.txt', 'labels': [
                    {'id': f'test_checks{number}.py::test_edge', 'part': number, 'label': 'Edge case', 'note': 'Check the edge.'}]}})
        workspace = self.directory / 'workspace'
        workspace.mkdir()
        (workspace / 'solution.py').write_text('PRIVATE CANDIDATE CODE')
        (self.directory / '.examiner' / 'report.json').write_text('PRIVATE ASSESSMENT')
        self.write()

    def write(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_frozen_exercise_only_with_a_reference_and_labels_per_part(self):
        before = {path: path.read_bytes() for path in self.directory.rglob('*') if path.is_file()}
        pack = pack_round(self.directory, self.state)
        self.assertEqual(pack['format'], 'solvewright-round-v1')
        round = pack['round']
        self.assertEqual(round['files'], {'solution.py': '# starter'})
        self.assertEqual([part['solution'] for part in round['parts']], ['solution at part 1', 'solution at part 2'])
        self.assertEqual(round['parts'][1]['checks']['labels'][0]['note'], 'Check the edge.')
        self.assertEqual(round['parts'][0]['hints'], [])
        self.assertEqual(round['debrief'], 'What a strong design handles.')
        self.assertNotIn('PRIVATE', json.dumps(pack))
        self.assertEqual(before, {path: path.read_bytes() for path in self.directory.rglob('*') if path.is_file()})

    def test_unfinished_and_still_busy_rounds_are_refused(self):
        for changed in ({'finished': False}, {'busy': True}, {'ending': True}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'Finish the round'):
                pack_round(self.directory, {**self.state, **changed})

    def test_older_round_has_a_plain_actionable_error(self):
        (self.root / 'manifest.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or oversized exercise asset manifest.json.*Older rounds'):
            pack_round(self.directory, self.state)

    def test_missing_future_part_reference_is_refused(self):
        (self.root / 'solution2.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'solution2.txt'):
            pack_round(self.directory, self.state)
        self.manifest['parts'].pop()
        self.write()
        with self.assertRaisesRegex(ValueError, 'every planned part'):
            pack_round(self.directory, self.state)

    def test_assets_cannot_read_candidate_files_or_follow_symlinks(self):
        for path in ('../../workspace/solution.py', '../report.json', '/tmp/secret', 'linked.py'):
            self.manifest['files']['solution.py'] = path
            self.write()
            if path == 'linked.py':
                (self.root / path).symlink_to(self.directory / 'workspace' / 'solution.py')
            with self.subTest(path=path), self.assertRaises(ValueError):
                pack_round(self.directory, self.state)

    def test_cpp_round_uses_its_own_tests_and_reference(self):
        self.state['settings']['language'] = 'C++'
        self.manifest['files'] = {'solution.hpp': 'starter.py'}
        for part in self.manifest['parts']:
            part['tests']['path'] = part['tests']['path'].replace('.py', '.cpp')
            part['checks']['path'] = part['checks']['path'].replace('.py', '.cpp')
            part['checks']['labels'][0]['id'] = part['checks']['labels'][0]['id'].replace('.py', '.cpp')
        self.write()
        self.assertEqual(pack_round(self.directory, self.state)['round']['language'], 'cpp')

    def test_pack_refuses_ambiguous_files_and_oversized_assets(self):
        self.manifest['parts'][1]['files'] = {'solution.py': 'starter.py'}
        self.write()
        with self.assertRaisesRegex(ValueError, 'overwrites the solution'):
            pack_round(self.directory, self.state)
        self.manifest['parts'][1]['files'] = {}
        self.write()
        (self.root / 'starter.py').write_text('x' * 250001)
        with self.assertRaisesRegex(ValueError, 'oversized exercise asset'):
            pack_round(self.directory, self.state)

    def test_worker_serves_pack_without_a_body(self):
        runtime = Mock()
        runtime.pack.return_value = {'format': 'solvewright-round-v1'}
        self.assertEqual(dispatch(runtime, 'reader', '/sessions/' + self.state['id'] + '/pack', None), runtime.pack.return_value)
        runtime.pack.assert_called_once_with('reader', self.state['id'])
