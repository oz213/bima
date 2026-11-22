"""Offline safety checks for the operator-only one-command snapshot rollover."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/bima_registry_rollover_v2.py'
spec = importlib.util.spec_from_file_location('bima_registry_rollover_v2', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlanTests(unittest.TestCase):
    def test_first_cycle_auto_generated(self):
        state = {'cycle': None, 'ready': False, 'sha': None}
        cycle, resumed = module.decide_cycle(state, 'abcd', None)
        self.assertRegex(cycle, r'^registry-\d{8}T\d{6}Z-\d{6}$')
        self.assertFalse(resumed)

    def test_incomplete_publication_resumes_original_cycle(self):
        state = {'cycle': 'registry-20261122T120000Z', 'ready': False, 'sha': 'same'}
        self.assertEqual(module.decide_cycle(state, 'same', None), (state['cycle'], True))

    def test_unfinished_cycle_rejects_different_json(self):
        state = {'cycle': 'registry-old', 'ready': False, 'sha': 'old'}
        with self.assertRaisesRegex(module.CutoverError, 'different source'):
            module.decide_cycle(state, 'new', None)

    def test_unfinished_cycle_rejects_different_cycle_id(self):
        state = {'cycle': 'registry-old', 'ready': False, 'sha': 'old'}
        with self.assertRaisesRegex(module.CutoverError, 'resume as'):
            module.decide_cycle(state, 'old', 'registry-new')

    def test_identical_current_content_requires_explicit_repeat(self):
        state = {'cycle': 'registry-current', 'ready': True, 'sha': 'x'}
        with self.assertRaisesRegex(module.CutoverError, 'repeat-source'):
            module.decide_cycle(state, 'x', None)
        cid, resume = module.decide_cycle(state, 'x', None, repeat_source=True)
        self.assertNotEqual(cid, 'registry-current')
        self.assertFalse(resume)

    def test_read_state_no_migration_or_db_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'db.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY)')
            self.assertEqual(module.state_readonly(path)['cycle'], None)
            with sqlite3.connect(path) as db:
                db.executescript('''
                CREATE TABLE registry_snapshot_runs_v2
                  (cycle_id TEXT, source_sha256 TEXT, url_count INTEGER);
                CREATE TABLE registry_snapshot_active_v2
                  (singleton INTEGER, cycle_id TEXT, ready INTEGER);
                INSERT INTO registry_snapshot_runs_v2 VALUES ('cycle-1','digest',120000);
                INSERT INTO registry_snapshot_active_v2 VALUES (1,'cycle-1',0);
                ''')
            current = module.state_readonly(path)
            self.assertEqual(current, dict(cycle='cycle-1',ready=False,sha='digest',url_count=120000))

    def test_unit_args_and_paths_validate_input_and_db(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            def fake_command(argv, **kwargs):
                if '-p' in argv and 'ExecStart' in argv:
                    return ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 bima.py "
                            "--input urls.json --db multi/state/db.sqlite3 ; ignore_errors=no }")
                return str(app)
            with mock.patch.object(module, 'command', side_effect=fake_command):
                module.verify_unit_paths(app / 'urls.json', app / 'multi/state/db.sqlite3')
                with self.assertRaisesRegex(module.CutoverError, '--input points'):
                    module.verify_unit_paths(app / 'wrong.json', app / 'multi/state/db.sqlite3')


    def test_first_cutover_checks_old_urls_json_count_before_stopping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'multi/state').mkdir(parents=True)
            (root / 'tools').mkdir()
            (root / 'venv/bin').mkdir(parents=True)
            (root / 'backups').mkdir()
            (root / 'urls.json.next').write_bytes(b'new-snapshot')
            (root / 'urls.json').write_bytes(b'old-snapshot')
            (root / 'multi/state/crawler_production.sqlite3').write_bytes(b'dummy')
            (root / 'tools/bima_registry_snapshot_v2.py').touch()
            (root / 'venv/bin/python3').touch()
            inst = module.Rollover(module.parse_args(
                ['--app', str(root), '--backup-root', str(root / 'backups'),
                 '--skip-unit-check', '--min-urls', '1']))
            def fake_owner(*args, **kwargs):
                source = args[args.index('--source') + 1]
                source = Path(source)
                return json.dumps({'source_sha256': module.sha256_file(source),
                                   'unique_urls': 1500000 if source == inst.target else 100000,
                                   'invalid': 0})
            with mock.patch.object(inst, 'owner', side_effect=fake_owner), \
                 mock.patch.object(module, 'state_readonly', return_value=
                                   {'cycle': None, 'ready': False, 'sha': None, 'url_count': None}), \
                 mock.patch.object(module, 'service_state', return_value='active'):
                with self.assertRaisesRegex(module.CutoverError, '50%'):
                    inst.preflight()


class FakeRollover(module.Rollover):
    def __init__(self, root):
        args = module.parse_args(['--app', str(root), '--backup-root', str(root / 'backups')])
        super().__init__(args)
        self.initial = dict(cycle='old', ready=True, sha='oldhash')
        self.service_before = {s:'active' for s in module.SERVICES}
        self.cycle = 'new'
        self.events = []
        self.step = None
        self.db_state = dict(cycle='old', ready=True, sha='oldhash')
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b'new JSON')
        self.target.write_bytes(b'old JSON')

    def preflight(self):
        self.events.append('preflight')
        if self.step == 'preflight':
            raise module.CutoverError('invalid JSON')
        return module.sha256_file(self.source)

    def backup(self):
        self.events.append('backup')
        self.backup_dir = self.app / 'backups' / 'new'
        if self.step == 'backup':
            raise module.CutoverError('out of disk space')

    def invoke_tool(self, mode):
        self.events.append(mode)
        if self.step == 'apply_rollback':
            raise module.CutoverError('transaction rolled back')
        if self.step == 'apply_partial':
            self.db_state = dict(cycle='new', ready=False, sha='newhash')
            raise module.CutoverError('atomic file swap failed')
        self.db_state = dict(cycle='new', ready=True, sha='newhash')
        self.target.write_bytes(self.source.read_bytes())
        return {'active_pending': 100, 'protected_hosts': 5}

    def restored_state(self):
        return self.db_state['cycle'] == self.initial['cycle'] and self.db_state['ready'] == self.initial['ready']

    def restore_original_services(self):
        self.events.append('restore')
        for s in module.SERVICES:
            if self.service_before[s] == 'active':
                module.systemctl('start', s)

    def health(self):
        self.events.append('health')
        return '12345'

    def archive_staged(self):
        self.events.append('archive')
        return self.source


class ProcedureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = {s:'active' for s in module.SERVICES}
        self.actions = []

        def fake_service(name):
            return self.current[name]
        def fake_systemctl(action, name):
            self.actions.append((action, name))
            if action == 'stop':
                self.current[name] = 'inactive'
            if action == 'start':
                self.current[name] = 'active'
        self.stub = mock.patch.multiple(module, service_state=fake_service,
                                        systemctl=fake_systemctl)
        self.stub.start()
        self.addCleanup(self.stub.stop)

    def test_successful_cutover_stops_then_starts_services(self):
        inst = FakeRollover(self.root)
        newhash = module.sha256_file(inst.source)
        inst.invoke_tool = lambda mode: self._applied(inst, newhash)
        with mock.patch.object(module, 'state_readonly', lambda db: inst.db_state):
            inst.execute()
        self.assertEqual([a[0] for a in self.actions], ['stop','stop','start','start'])
        self.assertEqual(inst.events[-2:], ['health','archive'])

    @staticmethod
    def _applied(inst, newhash):
        inst.db_state = dict(cycle='new', ready=True, sha=newhash)
        inst.target.write_bytes(inst.source.read_bytes())
        return {'active_pending': 100, 'protected_hosts': 5}

    def test_validation_failure_never_stops_services(self):
        inst = FakeRollover(self.root)
        inst.step = 'preflight'
        with self.assertRaisesRegex(module.CutoverError, 'invalid JSON'):
            inst.execute()
        self.assertEqual(self.actions, [])

    def test_backup_failure_restores_original_services(self):
        inst = FakeRollover(self.root)
        inst.step = 'backup'
        with self.assertRaisesRegex(module.CutoverError, 'disk space'):
            inst.execute()
        self.assertEqual(self.current, {s:'active' for s in module.SERVICES})
        self.assertIn('restore', inst.events)
        self.assertNotIn('apply', inst.events)

    def test_apply_rollback_restores_original_services(self):
        inst = FakeRollover(self.root)
        inst.step = 'apply_rollback'
        with self.assertRaisesRegex(module.CutoverError, 'rolled back'):
            inst.execute()
        self.assertIn('restore', inst.events)
        self.assertNotIn('health', inst.events)

    def test_partial_publication_fails_closed_both_services_off(self):
        inst = FakeRollover(self.root)
        inst.step = 'apply_partial'
        with mock.patch.object(module, 'state_readonly', lambda db: inst.db_state):
            with self.assertRaisesRegex(module.CutoverError, 'swap failed'):
                inst.execute()
        self.assertTrue(all(v == 'inactive' for v in self.current.values()))
        self.assertNotIn('restore', inst.events)
        self.assertNotIn('health', inst.events)

    def test_second_service_stop_failure_restores_first(self):
        inst = FakeRollover(self.root)
        def interrupted_stop(action, name):
            if action == 'stop' and name == module.SERVICES[1]:
                raise module.CutoverError('systemd failed to stop monitor')
            self.actions.append((action,name))
            self.current[name] = 'inactive' if action == 'stop' else 'active'
        with mock.patch.object(module, 'systemctl', side_effect=interrupted_stop):
            with self.assertRaisesRegex(module.CutoverError, 'systemd failed'):
                inst.execute()
        self.assertEqual(self.current, {s:'active' for s in module.SERVICES})
        self.assertIn('restore', inst.events)


if __name__ == '__main__':
    unittest.main()
