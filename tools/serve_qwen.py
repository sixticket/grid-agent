#!/usr/bin/env python3
"""Print the archived Qwen vLLM server configuration; start only with --execute."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def command(python, port, config):
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    # Keep the virtualenv interpreter symlink: resolving it can select system Python.
    interpreter = str(Path(python).absolute())
    return [interpreter, '-m', 'vllm.entrypoints.openai.api_server',
            '--model', config['model'], '--revision', config['revision'],
            '--tokenizer-revision', config['revision'],
            '--served-model-name', config['served_model_name'], '--host', '127.0.0.1',
            '--port', str(port), '--tensor-parallel-size', '1',
            '--max-model-len', str(config['context_tokens']), '--max-num-seqs', '1',
            '--max-num-batched-tokens', '8192', '--enable-chunked-prefill',
            '--enable-prefix-caching', '--gpu-memory-utilization', '0.90',
            '--kv-cache-dtype', 'fp8_per_token_head', '--language-model-only',
            '--reasoning-parser', 'qwen3', '--enable-auto-tool-choice',
            '--tool-call-parser', 'qwen3_coder', '--generation-config', 'vllm',
            '--hf-overrides', json.dumps(config['hf_overrides'])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', default=sys.executable, help='Interpreter in the GPU vLLM environment')
    parser.add_argument('--port', type=int, default=1235)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'models/legacy/qwen/model.json').read_text())
    try:
        cmd = command(args.python, args.port, config)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.execute:
        print(json.dumps(dict(status='plan_only', command=cmd,
                              environment={'VLLM_ALLOW_LONG_MAX_MODEL_LEN': '1'}), indent=2))
        return 0
    version = subprocess.check_output([cmd[0], '-c', 'import vllm; print(vllm.__version__)'], text=True).strip()
    if version != config['vllm_version']:
        parser.error('Use the recorded vLLM version: ' + config['vllm_version'])
    env = os.environ.copy()
    env['VLLM_ALLOW_LONG_MAX_MODEL_LEN'] = '1'
    env['PATH'] = str(Path(cmd[0]).parent) + os.pathsep + env.get('PATH', '')
    os.execve(cmd[0], cmd, env)


if __name__ == '__main__':
    raise SystemExit(main())
