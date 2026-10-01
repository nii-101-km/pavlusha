"""Opt-in LM Studio probe; never imported by the unit suite.

Captures raw API responses and only newly appended backend logs per request.
Timing alone is deliberately not classified as evidence of prefix reuse.
"""
import argparse
import copy
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:1234/v1')
    parser.add_argument('--model', default='qwen/qwen3.8-27b')
    parser.add_argument('--log', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = args.base_url.removesuffix('/v1')
    with urllib.request.urlopen(root + '/api/v1/models', timeout=10) as response:
        (args.output / 'models.json').write_bytes(response.read())
    base = [
        {'role': 'system', 'content': 'Pavlusha isolated prefix probe. Answer the final question briefly.'},
        {'role': 'user', 'content': 'Reference data:\n' + '\n'.join(
            f'Record {i}: the stored value is amber.' for i in range(128))},
        {'role': 'user', 'content': 'What is the stored value? Reply with one word.'},
    ]
    changed = copy.deepcopy(base)
    changed[1]['content'] = changed[1]['content'].replace('amber', 'violet')
    removed = [base[0], base[2]]
    appended = base + [{'role': 'assistant', 'content': 'amber'},
                       {'role': 'user', 'content': 'Repeat that value.'}]
    cases = [('initial', base, {}), ('repeat', base, {}),
             ('append', appended, {}), ('edit_earlier', changed, {}),
             ('remove_earlier', removed, {}), ('stream', base, {'stream': True}),
             ('stream_repeat', base, {'stream': True}),
             # These are diagnostic hypotheses, NOT runtime-supported options.
             ('cache_false', base, {'cache_prompt': False}),
             ('after_cache_false', base, {}),
             ('unknown_control', base, {'pavlusha_nonexistent_control': False})]
    for name, messages, options in cases:
        offset = args.log.stat().st_size if args.log else 0
        payload = dict(model=args.model, messages=messages, temperature=0.1,
                       max_tokens=64, stream=False)
        payload.update(options)
        if payload['stream']:
            payload['stream_options'] = {'include_usage': True}
        request = urllib.request.Request(args.base_url + '/chat/completions',
                                         data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode()
        record = {'name': name, 'request': payload, 'response': raw,
                  'seconds': time.monotonic() - start}
        if args.log:
            with args.log.open('rb') as log:
                log.seek(offset)
                record['backend_log'] = log.read().decode(errors='replace')
        (args.output / (name + '.json')).write_text(json.dumps(record, indent=2))
        print(name, round(record['seconds'], 3), flush=True)


if __name__ == '__main__':
    main()
