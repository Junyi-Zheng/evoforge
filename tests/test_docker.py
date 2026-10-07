import os
import tempfile
import unittest
from pathlib import Path

from evoforge.sandbox import Sandbox


@unittest.skipUnless(os.environ.get('EVOFORGE_DOCKER_TESTS') == '1', 'Docker integration is opt-in')
class DockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = Sandbox('docker', 'evoforge-runtime:local')
        cls.runtime.preflight()

    def test_non_root_runtime_without_host_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            source = '''import os
import socket

def inspect():
    try:
        socket.create_connection(('1.1.1.1', 443), timeout=0.2)
        network = True
    except OSError:
        network = False
    try:
        open('/rootfs-probe', 'w').close()
        writable = True
    except OSError:
        writable = False
    return {'uid': os.getuid(), 'credential': os.environ.get('EVOFORGE_SENTINEL'),
            'network': network, 'writable': writable}
'''
            (root / 'solution.py').write_text(source)
            os.environ['EVOFORGE_SENTINEL'] = 'must-stay-on-host'
            try:
                result = self.runtime.invoke('solution', root, {'relative_path': 'solution.py',
                                              'symbol': 'inspect', 'cases': [{'args': [], 'kwargs': {}}]})
            finally:
                os.environ.pop('EVOFORGE_SENTINEL', None)
            self.assertEqual(result[0]['value'], {'uid': 65534, 'credential': None,
                                                'network': False, 'writable': False})

    def test_candidate_step_protocol(self):
        seed = Path(__file__).resolve().parents[1] / 'src' / 'evoforge' / 'seed'
        decision = self.runtime.invoke('step', seed, {'state': {}, 'pending_action': None})
        self.assertEqual(decision['action'], {'tool': 'list_files'})


if __name__ == '__main__':
    unittest.main()
