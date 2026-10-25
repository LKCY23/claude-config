import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.runtime_requirements import check_runtime
from claude_config.core.catalog_schema import validate_catalog
from test_profile_resources import ResourceFixture


class RuntimeRequirementsTests(ResourceFixture):
    def requirement(self):
        return {'bindings': {'node': {'kind': 'executable', 'required': True}},
                'runtime': {'kind': 'node', 'binding': 'node', 'major': 22, 'platforms': ['darwin'],
                            'architectures': ['arm64'], 'modules_abi': '127'}}

    def test_interpreter_probe_has_no_resource_script_or_inherited_node_options(self):
        package = SimpleNamespace(ref='fixture@test', item={'requires': self.requirement()})
        actual = {'major': 22, 'platform': 'darwin', 'architecture': 'arm64', 'modules_abi': '127'}
        with patch('claude_config.core.runtime_requirements.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(actual))) as run:
            result = check_runtime(package, {'node': '/explicit/node'})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(run.call_args.args[0][:2], ['/explicit/node', '-p'])
        self.assertNotIn('NODE_OPTIONS', run.call_args.kwargs['env'])

    def test_wrong_node_major_or_abi_blocks(self):
        package = SimpleNamespace(ref='fixture@test', item={'requires': self.requirement()})
        for major, abi in [(26, '127'), (22, '999')]:
            actual = {'major': major, 'platform': 'darwin', 'architecture': 'arm64', 'modules_abi': abi}
            with patch('claude_config.core.runtime_requirements.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(actual))):
                with self.assertRaises(ValueError):
                    check_runtime(package, {'node': '/explicit/node'})

    def test_schema_rejects_runtime_without_required_executable(self):
        self.add('fixture', 'plugin', {'.claude-plugin/plugin.json': '{}'}, '.claude-plugin/plugin.json', requires=self.requirement())
        validate_catalog(self.catalog)
        self.catalog['resources']['fixture']['requires']['bindings']['node']['required'] = False
        with self.assertRaises(ValueError):
            validate_catalog(self.catalog)
