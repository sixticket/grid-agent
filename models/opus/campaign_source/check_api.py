#!/usr/bin/env python3
"""Explicit launch-time provider check. Merely importing this file never calls an API."""

# Preserved campaign source reference.
if __name__ == '__main__':
    raise SystemExit('Reference only; use tools/run_campaign.py. Historical replay requires the original pinned archive.')

import datetime
import json
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent


def check_access():
    config = json.loads((BASE / 'model.json').read_text())
    sys.path.insert(0, str(BASE.parent))
    credential = {'source': 'environment', 'api_key_env': config['api_key_env']}
    if not os.environ.get(config['api_key_env']):
        raise ValueError('Set the provider API key environment variable')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    out = BASE / 'api_checks' / stamp
    out.mkdir(parents=True, exist_ok=False)
    report = dict(checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  requested_model=config['model'], provider=config['provider'],
                  requested_effort=config['effort'], output=str(out), credential_source=credential)
    keys = ('GRID_API_USAGE_LOG', 'GRID_API_RESPONSE_LOG', 'GRID_REASONING_EFFORT', 'GRID_ANTHROPIC_CACHE_TTL')
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(GRID_API_USAGE_LOG=str(out / 'api_usage.jsonl'),
                      GRID_API_RESPONSE_LOG=str(out / 'api_responses.jsonl'),
                      GRID_REASONING_EFFORT=config['effort'], GRID_ANTHROPIC_CACHE_TTL='5m')
    try:
        if config['provider'] == 'local_qwen':
            # Loopback readiness only: the caller must explicitly start/tunnel the pinned vLLM server.
            import urllib.request
            serving = json.loads((BASE / 'model_serving_config.json').read_text())
            base_url = serving.get('base_url', 'http://127.0.0.1:1235/v1').rstrip('/')
            from urllib.parse import urlsplit
            if urlsplit(base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('Qwen readiness endpoint must be loopback; use an explicit SSH tunnel for a remote GPU server')
            with urllib.request.urlopen(base_url + '/models', timeout=10) as response:
                models = json.load(response)
            if config['model'] not in {item['id'] for item in models['data']}:
                raise ValueError('Pinned Qwen served model is missing')
            report.update(status='passed', check_kind='local_model_list_only', endpoint=base_url,
                          model_revision_verified=False, inference_executed=False,
                          limitation='Serving alias readiness does not independently attest model weight revision; use the pinned serving script.')
        else:
            sys.path.insert(0, str(BASE / 'runtime_original/experiment/engine'))
            from providers import OpenAIChat, AnthropicChat
            if config['provider'] == 'openai':
                import openai
                chat = OpenAIChat(config['model'])
                chat.client = openai.OpenAI(timeout=60, max_retries=0)
                model = chat.client.models.retrieve(config['model'])
                report['model_retrieval'] = dict(id=model.id, owned_by=model.owned_by)
            else:
                import anthropic
                chat = AnthropicChat(config['model'])
                chat.client = anthropic.Anthropic(timeout=90, max_retries=0)
            chat.send_user('Call experiment_ready once with ready=true.')
            result = chat.step('Verify the experiment API connection.', tools=[{
                'name': 'experiment_ready', 'description': 'Confirm the experiment API connection.',
                'input_schema': {'type': 'object', 'properties': {'ready': {'type': 'boolean'}},
                                 'required': ['ready'], 'additionalProperties': False}}],
                force_tool='experiment_ready', max_tokens=2048)
            calls = result['tool_calls']
            if len(calls) != 1 or calls[0]['name'] != 'experiment_ready' or calls[0]['input'] != {'ready': True}:
                raise ValueError('Expected function call was not returned')
            from api_usage import summarize_ledger
            report.update(status='passed', check_kind='live_tool_call', response_model=result['raw']['model'],
                          response_id=result['raw']['id'], usage=summarize_ledger(out / 'api_usage.jsonl'))
            if config['provider'] == 'anthropic':
                from opus_protocol import validate_journals
                report['thinking_protocol'] = validate_journals(out)
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__,
                      error_code=getattr(exc, 'code', None) or type(exc).__name__,
                      http_status=getattr(exc, 'status_code', None),
                      message='Launch-time provider check failed; no experiment was launched.')
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for target in (out / 'result.json', BASE / 'api_access_check.json'):
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, indent=2) + '\n')
        tmp.replace(target)
    return report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Actually contact the provider; hosted checks make a small paid call.')
    args = parser.parse_args()
    if not args.execute:
        parser.exit(0, 'Preparation only. No API/server request was sent. Use --execute only when execution is requested.\n')
    result = check_access()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'passed' else 2)
