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
    'claude-desktop': ('4.16336.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-Du-Wqi0E.js': 'fcc1026c3634f62ae40581c680389b807c49e85626ff9989838fa0dc931d7f1a',
    }),
}

# Keep earlier reviewed interface signatures too.
PREVIOUS_AUDITS = {
    'claude-desktop': (('4.23118.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-BBUaTQ8d.js': '1ffb394eb87f77ec1e8949c4acb8c30e973fcb6897987133f46444560dcd3df1',
    }), ('4.4212.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-m4oMImJ-.js': '926694919e5ff5d05cb804945637ffbe6ae75bdeac5e461f30e7c3d5fa043ccb',
    }), ('4.29.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-C7NkbHFq.js': '92e8f69fb04f151845edc56f4246a0d3a2d2f91b311cfdb26e4f64edc43819e0',
    }), ('3.6556.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-jPKPzV-P.js': '6e898798943b990e6eefa9209c61d977866b7898f681864f02c8cad3bb691ed5',
    }), ('2.42304.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-BkhOOUzl.js': '07d785c56978eb335c933562a514b3eba79cc5f307b4cd46f7f84e2dca5c83ec',
    }), ('2.42304.0', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-muI2IV6k.js': '4a8f1c07e69231e47127d3c55fcdda84eddb089ce06d347ad29591e25b6fb5f5',
    }), ('2.26454.2', 'com.anthropic.claudefordesktop', {
        '.vite/build/index.chunk-D86SM5R2.js': '6dc998c3796b86042889b5338b761f1b01f3254226f2f8f5d4b1faa69092c8f9',
    }),),
    'codex-app': (('26.1013.31256', 'com.openai.codex', {
        '.vite/build/src-Dtwb7yKT.js': 'a0e4b4b014f04a9391bcfa6a0d6f2b6703590daacf3591831a62fec809b2cb76',
        '.vite/build/startup-requirements-BlaoVssN.js': '8a38b0850e36b761e0d6f2b6703590716db41cb88de98c8e18fb392be25a38e6',
    }), ('26.1007.21159', 'com.openai.codex', {
        '.vite/build/src-CP7XXlpZ.js': '79623d50989f9efbcb8e5752c56b3cc608a0f5993c05112e03e11b718ac7a586',
        '.vite/build/startup-requirements-C6B1x9gL.js': '73f93b81152bff48cd27bcebcf27c430fb763e5c7418c67824950f4092ba933d',
    }), ('26.1002.52244', 'com.openai.codex', {
        '.vite/build/src-BPM2XJL0.js': '0f43ea10acf2852395ba10a8a24838bc5646b8543197e8eeadd1e4fadea4c7b1',
        '.vite/build/startup-requirements-BePnAl1c.js': 'a58e4e41184e552a4b337dcf8ca3e51c9338429c77957040d4efc37011bfb005',
    }),),
}

# These capabilities follow the exact matched source, never a numeric version comparison.
PACKED_CATALOG_AUDITS = frozenset(('4.29.0', '4.4212.0', '4.16336.0', '4.23118.0'))
DESKTOP_HOME_AUDITS = frozenset(('4.4212.0', '4.16336.0', '4.23118.0'))
_MAX_PROBE_MODULE_BYTES = 32 * 1024 * 1024


def _asar_header(content):
    require(len(content) >= 16, 'Invalid App source archive')
    header_size = struct.unpack_from('<I', content, 4)[0]
    json_size = struct.unpack_from('<I', content, 12)[0]
    require(16 + json_size <= 8 + header_size <= len(content), 'Invalid App source archive bounds')
    tree = mapping(json.loads(content[16:16 + json_size]), 'App source archive header')
    return header_size, tree


def _asar_read_node(content, header_size, node):
    require(not node.get('unpacked') and 'link' not in node
            and 'offset' in node and type(node.get('size')) is int and node['size'] >= 0,
            'Audited App source entry is missing')
    offset = 8 + header_size + int(node['offset'])
    require(offset >= 8 + header_size and offset + node['size'] <= len(content), 'App source entry escapes archive')
    return content[offset:offset + node['size']]


def asar_entries(content, selected):
    header_size, tree = _asar_header(content)
    result = {}
    for name in selected:
        node = tree
        for part in name.split('/'):
            node = mapping(mapping(node.get('files', {}), 'App source archive directory').get(part, {}), 'App source archive entry')
        result[name] = _asar_read_node(content, header_size, node)
    return result


def _asar_vite_js_entries(tree):
    """Return packed `.vite/build/**/*.js` entries inside an asar directory tree."""
    files_node = tree.get('files', {})
    if not isinstance(files_node, dict):
        return []
    vite = files_node.get('.vite')
    if not isinstance(vite, dict) or not isinstance(vite.get('files'), dict):
        return []
    build = vite['files'].get('build')
    if not isinstance(build, dict) or not isinstance(build.get('files'), dict):
        return []
    found = []
    def walk(dir_files, prefix):
        for name, node in sorted(dir_files.items()):
            if not isinstance(node, dict):
                continue
            rel = f'{prefix}/{name}'
            if isinstance(node.get('files'), dict):
                walk(node['files'], rel)
            elif (name.endswith('.js') and not node.get('unpacked') and 'link' not in node
                  and 'offset' in node and type(node.get('size')) is int and 0 <= node['size'] <= _MAX_PROBE_MODULE_BYTES):
                found.append((rel, node))
    walk(build['files'], '.vite/build')
    return found


def _semantic_probe_claude_desktop(content, header_size, tree, settings):
    """Locate Claude Desktop preference storage/schema contracts dynamically inside .vite/build/."""
    vite_entries = _asar_vite_js_entries(tree)
    if not vite_entries:
        return None
    main_modules = []
    main_blobs = []
    schema_modules = []
    schema_blobs = []
    for path, node in vite_entries:
        blob = _asar_read_node(content, header_size, node)
        if all(marker in blob for marker in (b'config.json', b'userThemeMode', b'configLibrary', b'_meta.json')):
            main_modules.append(path)
            main_blobs.append(blob)
        elif path == '.vite/build/managedConfigSchemas.js' or b'managedConfigSchemas' in blob or (
            b'desktopHome' in blob and b'chatTabEnabled' in blob
        ):
            schema_modules.append(path)
            schema_blobs.append(blob)
    if not main_modules:
        return None

    combined_blobs = (*main_blobs, *schema_blobs)
    for key in settings:
        if key == 'locale':
            if not any(b'i18n' in blob or b'locale' in blob for blob in main_blobs):
                return {'missing_field': key}
            continue
        marker = key.encode('ascii')
        if not any(marker in blob for blob in combined_blobs):
            return {'missing_field': key}

    desktop_home_supported = any(b'desktopHome' in blob for blob in combined_blobs)
    resources_files = (tree.get('files') or {}).get('resources', {}).get('files', {})
    i18n_files = resources_files.get('i18n', {}).get('files') if isinstance(resources_files, dict) else None
    packed_catalog = isinstance(i18n_files, dict)
    return {
        'matched_modules': [*main_modules, *schema_modules],
        'desktop_home_supported': desktop_home_supported,
        'packed_catalog': packed_catalog,
    }


def _semantic_probe_codex_app(content, header_size, tree, settings):
    """Locate Codex App desktop preference contracts dynamically inside .vite/build/."""
    vite_entries = _asar_vite_js_entries(tree)
    if not vite_entries:
        return None
    pref_modules = []
    pref_blobs = []
    markers = (b'appearanceTheme', b'composerEnterBehavior', b'followUpQueueMode')
    for path, node in vite_entries:
        blob = _asar_read_node(content, header_size, node)
        if any(marker in blob for marker in markers) or (b'config.toml' in blob and b'desktop' in blob):
            pref_modules.append(path)
            pref_blobs.append(blob)
    if not any(any(marker in blob for marker in markers) for blob in pref_blobs):
        return None
    for key in settings:
        marker = key.encode('ascii')
        if not any(marker in blob for blob in pref_blobs):
            return {'missing_field': key}
    return {'matched_modules': pref_modules}


def _validate_locale_catalog(application, archive_data, locale, *, packed, sources):
    """Verify bundled locale catalog JSON exists and parses as a JSON object."""
    if packed:
        name = 'resources/i18n/' + locale + '.json'
        catalog = asar_entries(archive_data, [name])[name]
        require(0 < len(catalog) <= 8 * 1024 * 1024 and isinstance(json.loads(catalog), dict),
                'Selected Claude interface locale has no valid packed catalog')
    else:
        catalog = application / 'Contents' / 'Resources' / (locale + '.json')
        raw, expected = snapshot_read(catalog)
        require(expected['exists'], 'Selected Claude interface locale has no bundled catalog')
        require(0 < len(raw) <= 8 * 1024 * 1024 and isinstance(json.loads(raw), dict),
                'Selected Claude interface locale has no valid bundled catalog')
        sources[catalog] = expected


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

            if reviewed_version is not None:
                if app == 'claude-desktop' and 'desktopHome' in settings:
                    stage = 'preference_field'
                    require(reviewed_version in DESKTOP_HOME_AUDITS,
                            'desktopHome has not been reviewed for this source interface')
                if app == 'claude-desktop' and isinstance(settings.get('locale'), str):
                    stage = 'locale_catalog'
                    # Selected by the matched source signature, not the build label.
                    _validate_locale_catalog(
                        application, data, settings['locale'],
                        packed=(reviewed_version in PACKED_CATALOG_AUDITS), sources=sources,
                    )
                checks.append({
                    'name': 'app_preference_interface', 'status': 'passed',
                    'version': version, 'reviewed_version': reviewed_version,
                    'level': 'installed-source-audit',
                    'reason': 'Selected preference storage and schemas match reviewed installed code',
                })
                return checks

            # L2 semantic source probe when exact Vite chunk hashes change across App updates.
            header_size, tree = _asar_header(data)
            semantic = (
                _semantic_probe_claude_desktop(data, header_size, tree, settings)
                if app == 'claude-desktop'
                else _semantic_probe_codex_app(data, header_size, tree, settings)
            )
            require(semantic is not None, 'App source differs from the audited preference interface')
            if 'missing_field' in semantic:
                stage = 'preference_field'
                require(False, f"Selected field {semantic['missing_field']} is absent from App preference interface")
            if app == 'claude-desktop' and 'desktopHome' in settings:
                stage = 'preference_field'
                require(semantic['desktop_home_supported'], 'desktopHome has not been verified in this source interface')
            if app == 'claude-desktop' and isinstance(settings.get('locale'), str):
                stage = 'locale_catalog'
                _validate_locale_catalog(
                    application, data, settings['locale'],
                    packed=semantic['packed_catalog'], sources=sources,
                )
            checks.append({
                'name': 'app_preference_interface', 'status': 'passed',
                'version': version, 'reviewed_version': None,
                'level': 'semantic-source-probe',
                'matched_modules': semantic['matched_modules'],
                'desktop_home_supported': bool(semantic.get('desktop_home_supported')),
                'reason': 'Selected preference storage and field contracts verified via semantic source probe',
            })
        except (OSError, ValueError, TypeError, KeyError, struct.error):
            reasons = {
                'application_metadata': '无法读取应用版本元数据；请核对绑定的应用路径',
                'application_identity': '应用身份不在已核对的兼容记录中',
                'preference_interface': '已安装应用的偏好接口源码未匹配审查记录；需核对接口后登记签名',
                'preference_field': '所选字段尚未在匹配的应用接口中审查；不能写入此字段',
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
