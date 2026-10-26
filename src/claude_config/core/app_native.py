"""Read-only audited App capability checks; never launch or stop the GUI."""
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import subprocess

from .native import snapshot_read
from .schema import mapping, require
from .transaction import PlanStale


# Reviewed preference interfaces. Build labels are evidence, not compatibility gates.
AUDITS = {
    'codex-app': ('26.1021.31345', 'com.openai.codex', {
        '.vite/build/src-CTNXdakG.js': '8bef484d79c728e944bf1ee5bc9d91e135864363ad449a3c182f0d888a991660',
        '.vite/build/startup-requirements-CuyNnLmn.js': '3451b138221aaad11aad1db8ec122a3363189379b9d47d2b0fe7bf553636a46f',
        '.vite/build/zod-DDAr8jEZ.js': 'e21ef21d2a511d5fa0b276b4f759011baf5b21d99d11e82a7c4515996eb4e0db',
        '.vite/build/rolldown-runtime-CPUxUITh.js': '9a875a54cb52a6ee0d1097895ae92aa8c9d999af95f167e90456d6fc0c03f296',
    }),
    'claude-desktop': ('3.6556.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-jPKPzV-P.js': '6e898798943b990e6eefa9209c61d977866b7898f681864f02c8cad3bb691ed5',
    }),
}

# Keep earlier reviewed interface signatures too.
PREVIOUS_AUDITS = {
    'claude-desktop': (('2.42304.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-BkhOOUzl.js': '07d785c56978eb335c933562a514b3eba79cc5f307b4cd46f7f84e2dca5c83ec',
    }), ('2.42304.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-muI2IV6k.js': '4a8f1c07e69231e47127d3c55fcdda84eddb089ce06d347ad29591e25b6fb5f5',
    }), ('2.26454.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-D86SM5R2.js': '6dc998c3796b86042889b5338b761f1b01f3254226f2f8f5d4b1faa69092c8f9',
    }),),
    'codex-app': (('26.1013.31256', 'com.openai.codex', {
        '.vite/build/src-Dtwb7yKT.js': 'a0e4b4b014f04a9391bcfa6a0d6f2b6703590daacf3591831a62fec809b2cb76',
        '.vite/build/startup-requirements-BlaoVssN.js': '8a38b0850e36b761e0d95e07e93600716db41cb88de98c8e18fb392be25a38e6',
    }), ('26.1007.21159', 'com.openai.codex', {
        '.vite/build/src-CP7XXlpZ.js': '79623d50989f9efbcb8e5752c56b3cc608a0f5993c05112e03e11b718ac7a586',
        '.vite/build/startup-requirements-C6B1x9gL.js': '73f93b81152bff48cd27bcebcf27c430fb763e5c7418c67824950f4092ba933d',
    }), ('26.1002.52244', 'com.openai.codex', {
        '.vite/build/src-BPM2XJL0.js': '0f43ea10acf2852395ba10a8a24838bc5646b8543197e8eeadd1e4fadea4c7b1',
        '.vite/build/startup-requirements-BePnAl1c.js': 'a58e4e41184e552a4b337dcf8ca3e51c9338429c77957040d4efc37011bfb005',
    }),),
}


def asar_entries(content, selected):
    require(len(content) >= 16, 'Invalid App source archive')
    header_size = struct.unpack_from('<I', content, 4)[0]
    json_size = struct.unpack_from('<I', content, 12)[0]
    require(16 + json_size <= 8 + header_size <= len(content), 'Invalid App source archive bounds')
    tree = mapping(json.loads(content[16:16 + json_size]), 'App source archive header')
    result = {}
    for name in selected:
        node = tree
        for part in name.split('/'):
            node = mapping(mapping(node.get('files', {}), 'App source archive directory').get(part, {}), 'App source archive entry')
        require('offset' in node and type(node.get('size')) is int and node['size'] >= 0,
                'Audited App source entry is missing')
        offset = 8 + header_size + int(node['offset'])
        require(offset >= 8 + header_size and offset + node['size'] <= len(content), 'App source entry escapes archive')
        result[name] = content[offset:offset + node['size']]
    return result


class AppProbe:
    def check(self, app, application, settings, sources):
        checks = []
        if not settings:
            return checks
        application = Path(application)
        stage, version = 'application_metadata', None
        try:
            info_path = application / 'Contents' / 'Info.plist'
            data, expected = snapshot_read(info_path)
            sources[info_path] = expected
            info = plistlib.loads(data)
            version = info.get('CFBundleShortVersionString')
            require(isinstance(version, str) and bool(version), 'App build metadata is missing')
            stage = 'application_identity'
            audits = [row for row in (AUDITS[app], *PREVIOUS_AUDITS.get(app, ()))
                      if info.get('CFBundleIdentifier') == row[1]]
            require(bool(audits), 'App identity has not been audited')
            stage = 'preference_interface'
            archive = application / 'Contents' / 'Resources' / 'app.asar'
            data, expected = snapshot_read(archive)
            sources[archive] = expected
            reviewed_version = None
            for baseline, _, hashes in audits:
                try:
                    entries = asar_entries(data, hashes)
                except (ValueError, KeyError):
                    continue
                if all(hashlib.sha256(entries[name]).hexdigest() == digest for name, digest in hashes.items()):
                    reviewed_version = baseline
                    break
            require(reviewed_version is not None, 'App source differs from the audited preference interface')
            if app == 'claude-desktop' and isinstance(settings.get('locale'), str):
                stage = 'locale_catalog'
                # The native setter silently falls back for missing catalogs;
                # do not claim that an unsupported locale was applied.
                catalog = application / 'Contents' / 'Resources' / (settings['locale'] + '.json')
                _, expected = snapshot_read(catalog)
                require(expected['exists'], 'Selected Claude interface locale has no bundled catalog')
                sources[catalog] = expected
            checks.append({'name': 'app_preference_interface', 'status': 'passed', 'version': version, 'reviewed_version': reviewed_version,
                           'level': 'installed-source-audit', 'reason': 'Selected preference storage and schemas match reviewed installed code'})
        except (OSError, ValueError, TypeError, KeyError, struct.error):
            reasons = {
                'application_metadata': '无法读取应用版本元数据；请核对绑定的应用路径',
                'application_identity': '应用身份不在已核对的兼容记录中',
                'preference_interface': '已安装应用的偏好接口源码未匹配审查记录；需核对接口后登记签名',
                'locale_catalog': '应用未提供所选语言包；请核对语言选择',
            }
            checks.append({'name': 'app_preference_interface', 'status': 'blocked', 'code': 'capability_unknown',
                           'stage': stage, 'version': version if isinstance(version, str) else None,
                           'reason': reasons[stage]})
        return checks

    def assert_stopped(self, application):
        self._assert_stopped(application)

    def _assert_stopped(self, application):
        # Both Electron main and helper processes can retain preference state.
        # No process names, command lines, arguments or environment are reported.
        result = subprocess.run(['/bin/ps', '-axo', 'command='], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise PlanStale('Cannot determine App lifecycle; close the App and replan')
        prefix = str(Path(application).resolve()) + '/Contents/'
        if any(line.lstrip().startswith(prefix) for line in result.stdout.splitlines()):
            raise PlanStale('App is running; quit it normally and replan before writing preferences')
