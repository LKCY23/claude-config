"""Probe declared interpreter capabilities without executing resource code."""
import json
import subprocess
import tempfile

from .schema import require

NODE_PROBE = 'JSON.stringify({major:Number(process.versions.node.split(".")[0]),platform:process.platform,architecture:process.arch,modules_abi:process.versions.modules})'


def check_runtime(package, slots):
    declaration = package.item['requires'].get('runtime')
    if not declaration:
        return None
    slot = declaration['binding']
    require(slot in slots, 'Resource runtime requires an explicit executable binding: ' + slot)
    with tempfile.TemporaryDirectory(prefix='claude-config-runtime-') as home:
        result = subprocess.run([slots[slot], '-p', NODE_PROBE], cwd=home,
                                env={'HOME': home, 'PATH': '/usr/bin:/bin', 'TMPDIR': home},
                                capture_output=True, text=True, timeout=10)
    require(result.returncode == 0, 'Bound Node runtime probe failed: ' + slot)
    try:
        actual = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('Bound Node runtime returned invalid capability data') from None
    require(isinstance(actual, dict), 'Invalid runtime probe response')
    require(actual.get('major') == declaration['major'], 'Node major version does not match the fixed resource requirement')
    require(actual.get('platform') in declaration['platforms'], 'Resource does not support this operating system')
    require(actual.get('architecture') in declaration['architectures'], 'Resource does not support this architecture')
    require(actual.get('modules_abi') == declaration['modules_abi'], 'Node native-module ABI does not match the fixed resource')
    return {'name': 'resource_runtime_dependency', 'ref': package.ref, 'status': 'passed',
            'binding': slot, 'actual': actual, 'reason': 'Interpreter capabilities checked; resource code and account access were not executed'}
