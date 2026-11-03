"""Translate the local experiment's Responses items to llama.cpp Chat Completions.

All user text, tool results, assistant text and reasoning are preserved in order.
The experiment still receives Responses-shaped records for its recovery engine.
"""
import json
import os
import time
import uuid
import openai
from openai._models import construct_type


def to_messages(instructions, items):
    messages = [{'role': 'system', 'content': instructions}]
    assistant = None

    def flush():
        nonlocal assistant
        if assistant is not None:
            if not assistant.get('content'):
                assistant['content'] = None
            messages.append(assistant)
            assistant = None

    def current():
        nonlocal assistant
        if assistant is None:
            assistant = {'role': 'assistant', 'content': ''}
        return assistant

    for item in items:
        kind = item.get('type', 'message')
        role = item.get('role')
        if kind == 'reasoning':
            parts = item.get('summary', []) + (item.get('content') or [])
            texts = []
            for part in parts:
                if part.get('text'):
                    texts.append(part['text'])
            if item.get('encrypted_content'):
                raise ValueError('Cannot translate encrypted reasoning to llama.cpp')
            a = current()
            a['reasoning_content'] = a.get('reasoning_content', '') + '\n'.join(texts)
        elif kind == 'function_call':
            a = current()
            a.setdefault('tool_calls', []).append({'id': item['call_id'], 'type': 'function', 'function': {'name': item['name'], 'arguments': item['arguments']}})
        elif kind == 'function_call_output':
            flush()
            output = item['output']
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': output if isinstance(output, str) else json.dumps(output)})
        elif kind == 'message' and role in ('user', 'assistant', 'system', 'developer'):
            content = item.get('content', '')
            if isinstance(content, list):
                if any(x.get('type') not in ('input_text', 'output_text', 'text') for x in content):
                    raise ValueError('Only text content is supported by this experiment adapter')
                content = ''.join(x['text'] for x in content)
            if role == 'assistant':
                current()['content'] += content or ''
            else:
                flush()
                messages.append({'role': role, 'content': content})
        else:
            raise ValueError(f'Unsupported Responses item: {kind}/{role}')
    flush()
    return messages


def create_response(client, **kw):
    messages = to_messages(kw['instructions'], kw['input'])
    args = {'model': kw['model'], 'messages': messages, 'max_tokens': kw['max_output_tokens']}
    if kw.get('tools'):
        args['tools'] = [{'type': 'function', 'function': {k: t[k] for k in ('name', 'description', 'parameters', 'strict') if k in t}} for t in kw['tools']]
    choice = kw.get('tool_choice')
    if isinstance(choice, dict):
        # This server accepts string tool_choice. Restricting the tool list
        # and requiring a call preserves the named-tool constraint.
        args['tools'] = [t for t in args.get('tools', []) if t['function']['name'] == choice['name']]
        if len(args['tools']) != 1:
            raise ValueError('Forced tool must name exactly one supplied tool')
        args['tool_choice'] = 'required'
    elif choice is not None:
        args['tool_choice'] = choice
    # Prevent context shifting; keep reasoning in subsequent request templates.
    args['extra_body'] = {'cache_prompt': True, 'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}, 'reasoning_format': 'deepseek'}
    if os.environ.get('GRID_LOCAL_BACKEND') == 'vllm-chat':
        args['extra_body'] = {'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}, 'top_k': 20, 'min_p': 0.0, 'repetition_penalty': 1.0}
        args.update(temperature=1.0, top_p=0.95)
    r = client.chat.completions.create(**args)
    choice = r.choices[0]
    m = choice.message
    mid = uuid.uuid4().hex
    output = []
    reasoning = getattr(m, 'reasoning_content', None) or getattr(m, 'reasoning', None)
    if reasoning:
        output.append({'type': 'reasoning', 'id': 'rs_' + mid, 'summary': [{'type': 'summary_text', 'text': reasoning}]})
    if m.content:
        output.append({'type': 'message', 'role': 'assistant', 'id': 'msg_' + mid, 'status': 'completed', 'content': [{'type': 'output_text', 'text': m.content, 'annotations': []}]})
    for tc in m.tool_calls or []:
        output.append({'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex, 'call_id': tc.id, 'name': tc.function.name, 'arguments': tc.function.arguments, 'status': 'completed'})
    complete = choice.finish_reason in ('stop', 'tool_calls')
    details = r.usage.prompt_tokens_details
    cached = getattr(details, 'cached_tokens', 0) if details else 0
    raw = {'id': 'resp_' + mid, 'object': 'response', 'created_at': time.time(), 'model': kw['model'], 'status': 'completed' if complete else 'incomplete', 'error': None, 'incomplete_details': None if complete else {'reason': 'max_output_tokens' if choice.finish_reason == 'length' else choice.finish_reason}, 'instructions': kw['instructions'], 'output': output, 'usage': {'input_tokens': r.usage.prompt_tokens, 'output_tokens': r.usage.completion_tokens, 'total_tokens': r.usage.total_tokens, 'input_tokens_details': {'cached_tokens': cached}, 'output_tokens_details': {'reasoning_tokens': getattr(r.usage.completion_tokens_details, 'reasoning_tokens', None)}}, 'local_backend_response': r.model_dump()}
    return construct_type(type_=openai.types.responses.Response, value=raw)
