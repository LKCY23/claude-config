"""Initialize an isolated Claude process without submitting a user/model turn."""
import json
import selectors
import subprocess
import time


def initialize(binary, cwd, env, timeout=20):
    process = subprocess.Popen(
        [binary, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
         '--verbose', '--permission-prompt-tool', 'stdio'], cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    try:
        process.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'discovery-only',
                                       'request': {'subtype': 'initialize'}}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                line = process.stdout.readline()
                if not line:
                    break
                event = json.loads(line)
                response = event.get('response', {})
                if event.get('type') == 'control_response' and response.get('request_id') == 'discovery-only':
                    if response.get('subtype') != 'success':
                        raise AssertionError('Claude initialize did not succeed')
                    return response.get('response', {})
        raise AssertionError('Claude initialize did not return within the bounded probe')
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)
        process.stdin.close(); process.stdout.close()
