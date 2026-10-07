import json
import tempfile
import unittest
from pathlib import Path

from evoforge.archive import Archive
from evoforge.engine import Engine
from evoforge.models import Config
from evoforge.report import export_report


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name) / 'run'
        cls.engine = Engine.create(cls.root, Config(rounds=3), progress=lambda message: None)
        cls.addClassCleanup(cls.engine.archive.close)
        cls.engine.evolve()

    def test_branching_and_artifacts(self):
        state = self.engine.state
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(state['nodes']), 4)
        self.assertEqual({n['parent'] for n in state['nodes'][1:]}, {'c0000'})
        self.assertTrue(self.engine.archive.audit()['ok'])
        for node in state['nodes'][1:]:
            self.assertTrue((self.engine.archive.candidate_dir(node['id']) / 'change.diff').read_text())

    def test_run_lock(self):
        with self.assertRaisesRegex(ValueError, 'in use'):
            Archive(self.root)

    def test_local_rejects_custom_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'require Docker'):
                Engine.create(Path(tmp) / 'run', Config(), suite=Path('custom.json'))

    def test_tamper_detection(self):
        node = self.engine.state['nodes'][0]
        path = self.engine.archive.candidate_dir(node['id']) / 'change.diff'
        original = path.read_text()
        try:
            path.write_text('tampered')
            self.assertFalse(self.engine.archive.audit()['ok'])
        finally:
            path.write_text(original)
        self.assertTrue(self.engine.archive.audit()['ok'])

    def test_report_escapes_embedded_html(self):
        state = self.engine.state
        original = state['nodes'][0]['summary']
        try:
            state['nodes'][0]['summary'] = '</script><script>alert(1)</script>'
            self.engine.archive.commit(state, 'report_escape_fixture')
            path = export_report(self.engine.archive)
            html = path.read_text()
            self.assertNotIn('</script><script>alert(1)</script>', html)
            self.assertIn('\\u003c/script>', html)
        finally:
            state['nodes'][0]['summary'] = original
            self.engine.archive.commit(state, 'report_escape_restored')

    def test_z_seal_is_idempotent_and_closes_evolution(self):
        result = self.engine.seal()
        calls = self.engine.ledger.usage.calls
        self.assertEqual(result, self.engine.seal())
        self.assertEqual(calls, self.engine.ledger.usage.calls)
        with self.assertRaisesRegex(ValueError, 'closed'):
            self.engine.evolve()
        self.assertTrue(self.engine.archive.audit()['ok'])


if __name__ == '__main__':
    unittest.main()
