import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from evoforge.archive import Archive
from evoforge.benchmarks import builtin_suite, safe_path, suite_dict, validate_suite
from evoforge.budget import BudgetExceeded, Ledger
from evoforge.evaluation import Evaluator, equal
from evoforge.genome import InvalidGenome, ablate, apply_edits, read_genome, validate, write_genome
from evoforge.models import Case, Config, Limits, Usage, canonical
from evoforge.providers import ChatProvider, ProviderFailure, parse_object
from evoforge.sandbox import RuntimeFailure, Sandbox
from evoforge.selection import champion, pareto_front, regression_gate

SEED = Path(__file__).resolve().parents[1] / 'src' / 'evoforge' / 'seed'


class GenomeTests(unittest.TestCase):
    def setUp(self):
        self.seed = read_genome(SEED)

    def test_component_boundary_and_noop(self):
        with self.assertRaises(InvalidGenome):
            apply_edits(self.seed, [{'path': 'agent.py', 'content': self.seed['agent.py']}], 'memory')
        with self.assertRaises(InvalidGenome):
            apply_edits(self.seed, [{'path': 'memory.py', 'content': self.seed['memory.py']}], 'memory')

    def test_source_changes_and_reversion(self):
        child, diff = apply_edits(self.seed, [{'path': 'memory.py', 'content': 'def render(trace, max_chars=12000):\n    return trace[-4:]\n'}], 'memory')
        self.assertIn('parent/memory.py', diff)
        self.assertEqual(ablate(child, self.seed, 'memory'), self.seed)
        self.assertNotEqual(child, self.seed)

    def test_reject_host_access_and_invalid_entry_points(self):
        for source in ['import os\n', 'x = open("/etc/passwd")\n', 'x = (1).__class__\n', 'from . import tools\n']:
            with self.subTest(source=source), self.assertRaises(InvalidGenome):
                validate({**self.seed, 'tools.py': source})
        with self.assertRaises(InvalidGenome):
            validate({**self.seed, 'agent.py': 'def step(observation):\n    return {}\n'})

    def test_manifest_rejects_symlinks_and_extra_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'genome'
            write_genome(root, self.seed)
            (root / 'unexpected').write_text('extra')
            with self.assertRaises(InvalidGenome):
                read_genome(root)
            (root / 'unexpected').unlink()
            (root / 'tools.py').unlink()
            (root / 'tools.py').symlink_to(SEED / 'tools.py')
            with self.assertRaises(InvalidGenome):
                read_genome(root)

    def test_genome_modules_can_import_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            genome = {**self.seed,
                      'tools.py': 'import memory\ndef value():\n    return memory.value()\n',
                      'memory.py': 'def value():\n    return 42\n',
                      'agent.py': 'import tools\ndef step(observation):\n    return tools.value()\ndef propose(context):\n    return {}\n'}
            root = Path(tmp) / 'genome'
            write_genome(root, genome)
            self.assertEqual(Sandbox('local', '').invoke('step', root, {}), 42)


class BudgetTests(unittest.TestCase):
    def test_reservation_is_durable_and_settles(self):
        saved = []
        ledger = Ledger(Config(total_tokens=100, total_calls=2))
        ledger.on_change = lambda: saved.append(ledger.summary())
        ledger.reserve(10, 20)
        self.assertEqual(saved[-1]['tokens'], 30)
        restored = Ledger(ledger.config)
        restored.restore(saved[-1]['entries'])
        self.assertEqual(restored.usage.tokens, 30)
        ledger.record('request', Usage(1, 4, 8))
        self.assertEqual(ledger.usage.tokens, 12)
        self.assertFalse(ledger.entries[0]['pending'])

    def test_cannot_spend_beyond_or_double_reserve(self):
        ledger = Ledger(Config(total_tokens=30, total_calls=1))
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(15, 20)
        ledger.reserve(10, 20)
        with self.assertRaises(RuntimeError):
            ledger.reserve(1, 1)
        ledger.record('used', Usage(1, 5, 5))
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(1, 1)

    def test_provider_overage_stays_charged(self):
        ledger = Ledger(Config(total_tokens=30))
        ledger.reserve(5, 10)
        with self.assertRaises(BudgetExceeded):
            ledger.record('overage', Usage(1, 20, 20))
        self.assertEqual(ledger.usage.tokens, 40)


class ProviderTests(unittest.TestCase):
    def provider(self):
        config = Config()
        return ChatProvider(config, Ledger(config))

    @staticmethod
    def response(content='{"tool":"finish"}', usage=None):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return canonical({'choices': [{'message': {'content': content}}],
                                  'usage': usage or {'prompt_tokens': 12, 'completion_tokens': 4}}).encode()
        return Response()

    def test_usage_is_recorded_for_malformed_model_json(self):
        provider = self.provider()
        with patch('urllib.request.urlopen', return_value=self.response('not JSON')):
            with self.assertRaises(ProviderFailure):
                provider.complete({'system': 'Return JSON.', 'payload': {}}, 20, 'unit')
        self.assertEqual(provider.ledger.usage.tokens, 16)
        self.assertEqual(provider.ledger.usage.calls, 1)

    def test_transport_failure_charges_reservation(self):
        provider = self.provider()
        with patch('urllib.request.urlopen', side_effect=TimeoutError('timeout')):
            with self.assertRaises(ProviderFailure):
                provider.complete({'system': 'Return JSON.', 'payload': {}}, 20, 'unit')
        self.assertTrue(provider.ledger.usage.estimated)
        self.assertEqual(provider.ledger.usage.calls, 1)
        self.assertFalse(provider.ledger.entries[-1]['pending'])

    def test_strict_reply_and_endpoint(self):
        self.assertEqual(parse_object('```json\n{"x": 1}\n```'), {'x': 1})
        for text in ['[]', '{broken', 'null']:
            with self.assertRaises(ProviderFailure):
                parse_object(text)
        with self.assertRaises(ValueError):
            ChatProvider(Config(base_url='http://remote.invalid/v1'), Ledger(Config()))


class EvaluationTests(unittest.TestCase):
    def test_boolean_and_numeric_results_are_distinct(self):
        self.assertFalse(equal(True, 1))
        self.assertFalse(equal(1, True))
        self.assertTrue(equal(1.0, 1))
        self.assertFalse(equal(float('nan'), 1))
        self.assertTrue(equal({'items': [1, 2.0]}, {'items': [1.0, 2]}))

    def test_grade_keeps_expected_values_out_of_worker(self):
        class RecordingRuntime:
            def invoke(self, mode, root, context, timeout=None):
                self.context = context
                return [{'value': 'secret-answer', 'error': '', 'args_after': ['input'], 'kwargs_after': {}}]
        runtime = RecordingRuntime()
        task = replace(builtin_suite()[0], public=[Case(['input'], 'secret-answer')])
        result = Evaluator(Config(), runtime, None).grade(Path('.'), task, task.public)
        self.assertTrue(result['all_passed'])
        self.assertNotIn('secret-answer', canonical(runtime.context))
        self.assertNotIn('expected', canonical(runtime.context))

    def test_input_mutation_is_detected(self):
        task = builtin_suite()[0]
        cases = [Case([[3, 1]], [1, 3], check_input_unchanged=True)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / task.target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def merge_intervals(items):\n    items.sort()\n    return items\n')
            grader = Evaluator(Config(), Sandbox('local', ''), None)
            self.assertFalse(grader.grade(root, task, cases)['all_passed'])
            self.assertTrue(grader.grade(root, task, [replace(cases[0], check_input_unchanged=False)])['all_passed'])

    def test_source_paths_and_split_integrity(self):
        for name in ['.', '../x.py', '/x.py', 'a/../x.py', 'a//b.py', 'a\\b.py', 'a\x00.py']:
            self.assertFalse(safe_path(name), name)
        self.assertTrue(safe_path('src/module.py'))
        tasks = builtin_suite()
        self.assertEqual(len(tasks), 24)
        self.assertEqual(suite_dict(tasks), suite_dict(builtin_suite()))
        with self.assertRaises(ValueError):
            validate_suite(tasks + [replace(tasks[0], id='duplicate')])

    def test_config_rejects_live_local_and_invalid_limits(self):
        for config in [Config(mode='live'), Config(rounds=10000), Config(limits=Limits(tokens=0)),
                       Config(limits=Limits(calls=1.5)), Config(exploration=float('nan'))]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                config.validate()


class SelectionTests(unittest.TestCase):
    @staticmethod
    def node(name, score, tokens, status='accepted'):
        return {'id': name, 'status': status, 'validation': {'score': score, 'mean_tokens': tokens}}

    def test_quality_cost_tradeoff_retains_multiple_branches(self):
        nodes = [self.node('c0000', .5, 200), self.node('c0001', .8, 300), self.node('c0002', .5, 300)]
        self.assertEqual(set(pareto_front(nodes)), {'c0000', 'c0001'})
        self.assertEqual(champion(nodes)['id'], 'c0001')

    def test_mean_improvement_does_not_hide_a_regressed_task(self):
        parent = {'score': .5, 'by_task': {'a': 1, 'b': 0, 'c': .5}}
        child = {'score': 2 / 3, 'by_task': {'a': 0, 'b': 1, 'c': 1}}
        gate = regression_gate(parent, child, 0, 0)
        self.assertGreater(gate['gain'], 0)
        self.assertFalse(gate['accepted'])
        self.assertEqual(gate['regressed_tasks'], ['a'])
        self.assertTrue(regression_gate(parent, child, .34, 0)['accepted'])

    def test_comparisons_require_paired_tasks(self):
        with self.assertRaises(ValueError):
            regression_gate({'score': 0, 'by_task': {'a': 0}}, {'score': 1, 'by_task': {'b': 1}}, 0, 0)


if __name__ == '__main__':
    unittest.main()
