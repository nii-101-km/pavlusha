"""Isolated local-provider evidence; never changes model configuration or runtime State."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pavlusha_agent.provider import ChatProvider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:1234/v1')
    parser.add_argument('--model', default='qwen/qwen3.8-27b')
    parser.add_argument('--output', default='context-guard-results/provider-overflow-probe.json')
    args = parser.parse_args()
    provider = ChatProvider(args.base_url, args.model, '', 120, 0, 16)
    evidence = {'base_url': args.base_url, 'model': args.model,
                'capacity': provider.discover_context_length(), 'max_tokens': 16}
    messages = [{'role': 'user', 'content': ' x' * 180000}]
    payload = {'model': args.model, 'messages': messages, 'max_tokens': 16, 'stream': False}
    request = urllib.request.Request(args.base_url + '/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            evidence['raw'] = {'status': response.status, 'body': json.load(response)}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')
        evidence['raw'] = {'status': exc.code, 'body': raw, 'exception': type(exc).__name__}
    payload['stream'] = True
    payload['stream_options'] = {'include_usage': True}
    request = urllib.request.Request(args.base_url + '/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            evidence['raw_stream'] = {'status': response.status, 'body': response.read().decode('utf-8', 'replace')}
    except urllib.error.HTTPError as exc:
        evidence['raw_stream'] = {'status': exc.code, 'body': exc.read().decode('utf-8', 'replace')}
    for mode in ['normal', 'stream']:
        try:
            turn = provider.worker_completion(messages, **({'on_delta': lambda *x: None} if mode == 'stream' else {}))
            evidence[mode] = {'unexpected_success': vars(turn)}
        except Exception as exc:
            evidence[mode] = {'exception': type(exc).__name__, 'message': str(exc),
                             'cause': type(exc.__cause__).__name__}
    turn = provider.worker_completion([{'role': 'user', 'content': 'Reply with OK.'}])
    evidence['normal_measurement'] = vars(turn)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(evidence, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
