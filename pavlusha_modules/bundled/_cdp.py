"""CDP diagnostics and explicitly enabled evaluation for one selected page."""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import queue
import socket
import threading
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from ._browser import EventBuffer


def safe_url(value):
    try:
        p = urlsplit(str(value))
        # Do not copy credentials, query strings or fragments into network logs.
        host = p.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if p.port:
            host += ':' + str(p.port)
        return urlunsplit((p.scheme, host, p.path, '', ''))[:2048]
    except ValueError:
        return '<invalid URL>'


def local_endpoint(value, scheme='http'):
    p = urlsplit(value)
    if p.scheme != scheme or p.hostname not in ('127.0.0.1', 'localhost', '::1') or p.username or p.password:
        raise ValueError('browser diagnostics endpoint must be loopback ' + scheme)
    if p.query or p.fragment:
        raise ValueError('endpoint query/fragment not allowed')
    _ = p.port  # validate port
    return p


class CdpDiagnosticsAdapter:
    def __init__(self, config):
        if set(config) - {'adapter', 'endpoint', 'target_id', 'target_url', 'allow_evaluate'}:
            raise ValueError('unknown CDP configuration fields')
        self.allow_evaluate = config.get('allow_evaluate', False)
        if type(self.allow_evaluate) is not bool:
            raise ValueError('allow_evaluate must be boolean')
        if bool(config.get('target_id')) == bool(config.get('target_url')):
            raise ValueError('specify exactly one target_id or exact target_url')
        endpoint = config.get('endpoint', 'http://127.0.0.1:9222').rstrip('/')
        ep = local_endpoint(endpoint)
        if ep.path not in ('', '/'):
            raise ValueError('CDP endpoint must have no path')
        self.buffer = EventBuffer()
        self.requests = OrderedDict()
        self.capabilities = []
        self.unsupported = {}
        self.state = 'connecting'
        self.error = None
        self.ws = None
        self.thread = None
        self.stop = threading.Event()
        self.seq = 0
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.network_time_offset = None
        try:
            # Bypass environment proxies for this explicit local-only connection.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(endpoint + '/json/list', timeout=5) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('target discovery response too large')
            targets = json.loads(raw)
            candidates = [t for t in targets if t.get('type') == 'page' and
                          (t.get('id') == config.get('target_id') if config.get('target_id') else
                           t.get('url') == config['target_url'])]
            if len(candidates) != 1:
                raise ValueError('selected page must match exactly one target; no fallback or auto-selection')
            target = candidates[0]
            self.target = {'id': target['id'], 'url': safe_url(target.get('url', ''))}
            ws_url = target['webSocketDebuggerUrl']
            wp = local_endpoint(ws_url, 'ws')
            if wp.hostname != ep.hostname or (wp.port or 80) != (ep.port or 80):
                raise ValueError('target WebSocket must use configured endpoint host and port')
            try:
                import websocket
            except ImportError as exc:
                raise ValueError('CDP adapter requires optional requirements-browser.txt (websocket-client)') from exc
            self.ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True,
                http_no_proxy=['127.0.0.1', 'localhost', '::1'], enable_multithread=True)
            for domain, kinds in [('Runtime', ['console', 'exception']),
                                  ('Network', ['request_failed', 'http_error']), ('Page', [])]:
                if self._enable(domain):
                    self.capabilities.extend(kinds)
                    if domain == 'Runtime' and self.allow_evaluate:
                        self.capabilities.append('evaluate')
            if not self.capabilities:
                raise ValueError('selected target offers no supported diagnostic domains')
            self.state = 'connected'
            self.ws.settimeout(0.5)
            self.thread = threading.Thread(target=self._receive, name='pavlusha-browser-diagnostics', daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _decode(self, raw):
        if not raw or len(raw) > 2 * 1024 * 1024:
            raise ValueError('empty or oversized CDP message')
        return json.loads(raw)

    def _enable(self, domain):
        self.seq += 1
        self.ws.send(json.dumps({'id': self.seq, 'method': domain + '.enable'}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = self._decode(self.ws.recv())
            if message.get('id') == self.seq:
                if 'error' in message:
                    self.unsupported[domain] = str(message['error'].get('message', 'unsupported'))[:300]
                    return False
                return True
            self._event(message)
        raise TimeoutError('CDP enable timed out')

    def _receive(self):
        import websocket
        try:
            while not self.stop.is_set():
                try:
                    message = self._decode(self.ws.recv())
                    if 'id' in message:
                        with self.pending_lock:
                            waiter = self.pending.get(message['id'])
                        if waiter is not None:
                            waiter.put_nowait(message)
                    else:
                        self._event(message)
                except (websocket.WebSocketTimeoutException, socket.timeout):
                    continue
        except Exception as exc:
            if not self.stop.is_set():
                self.error = type(exc).__name__ + ': ' + str(exc)[:300]
                self.state = 'disconnected'
        finally:
            self._wake_pending()

    def _wake_pending(self):
        with self.pending_lock:
            for waiter in self.pending.values():
                try:
                    waiter.put_nowait({'error': {'message': 'browser connection closed'}})
                except queue.Full:
                    pass

    def _command(self, method, params, timeout):
        if self.state != 'connected':
            raise ConnectionError('selected browser target is not connected')
        waiter = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.seq += 1
            command_id = self.seq
            self.pending[command_id] = waiter
        try:
            self.ws.send(json.dumps({'id': command_id, 'method': method, 'params': params}))
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError('browser command deadline exceeded') from exc
            if 'error' in response:
                raise RuntimeError(str(response['error'].get('message', 'CDP command failed'))[:600])
            return response.get('result', {})
        finally:
            with self.pending_lock:
                self.pending.pop(command_id, None)

    def evaluate(self, *, expression, timeout_ms=1000, await_promise=False):
        if not self.allow_evaluate or 'evaluate' not in self.capabilities:
            return {'error': 'browser_evaluate_disabled', 'execution_may_have_occurred': False}
        try:
            result = self._command('Runtime.evaluate', {
                'expression': expression, 'returnByValue': True, 'awaitPromise': await_promise,
                'timeout': timeout_ms, 'includeCommandLineAPI': False,
                'allowUnsafeEvalBlockedByCSP': False,
            }, timeout_ms / 1000 + 1)
            details = result.get('exceptionDetails')
            if details:
                return {'error': 'javascript_exception', 'executed': True,
                        'detail': str(details.get('exception', {}).get('description') or details.get('text', 'JavaScript exception'))[:4000],
                        'url': safe_url(details.get('url', '')), 'line': details.get('lineNumber'),
                        'column': details.get('columnNumber')}
            value = result.get('result', {})
            payload = {'executed': True, 'type': value.get('type', 'unknown')}
            if 'value' in value:
                payload['value'] = value['value']
            elif 'unserializableValue' in value:
                payload['unserializable_value'] = value['unserializableValue']
            elif value.get('type') != 'undefined':
                payload['description'] = str(value.get('description', 'value is not JSON-serializable'))[:4000]
            return payload
        except TimeoutError:
            terminated = False
            try:
                self._command('Runtime.terminateExecution', {}, 2)
                terminated = True
            except Exception:
                pass
            return {'error': 'evaluation_timeout', 'termination_requested': terminated,
                    'execution_may_have_occurred': True,
                    'detail': 'No automatic retry. Side effects are not rolled back; asynchronous work may remain.'}
        except Exception as exc:
            return {'error': 'browser_evaluation_failed', 'detail': str(exc)[:600],
                    'execution_may_have_occurred': True}

    def _event_time(self, method, params):
        """Expose epoch seconds, never mix Runtime milliseconds with Network ticks."""
        received = time.time()
        raw = params.get('timestamp')

        def valid(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0

        # CDP supplies both clocks on a request event. Do not assume the remote
        # browser's monotonic origin equals this controller's monotonic origin.
        if method == 'Network.requestWillBeSent' and valid(raw) and valid(params.get('wallTime')):
            self.network_time_offset = params['wallTime'] - raw
        stamp, source = received, 'received'
        if method.startswith('Runtime.') and valid(raw):
            stamp, source = raw / 1000, 'browser_epoch'
        elif method.startswith('Network.') and valid(raw):
            offset = getattr(self, 'network_time_offset', None)
            if offset is not None and valid(raw + offset):
                stamp, source = raw + offset, 'browser_monotonic'
        return {'timestamp': stamp, 'timestamp_source': source, 'received_at': received}

    def _event(self, message):
        method, p = message.get('method'), message.get('params', {})
        stamp = self._event_time(method or '', p)
        if method == 'Runtime.consoleAPICalled':
            args = p.get('args', [])[:20]
            parts = [str(a.get('value', a.get('unserializableValue', a.get('description', '<object>'))))[:1000] for a in args]
            self.buffer.add('console', **stamp, level=str(p.get('type', 'log'))[:40], text=' '.join(parts)[:4000])
        elif method == 'Runtime.exceptionThrown':
            details = p.get('exceptionDetails', {})
            text = details.get('exception', {}).get('description') or details.get('text', 'JavaScript exception')
            self.buffer.add('exception', **stamp, text=str(text)[:4000],
                url=safe_url(details.get('url', '')), line=details.get('lineNumber'), column=details.get('columnNumber'))
        elif method == 'Network.requestWillBeSent':
            request = p.get('request', {})
            self.requests[p['requestId']] = {'url': safe_url(request.get('url', '')), 'method': str(request.get('method', ''))[:16]}
            while len(self.requests) > 1000:
                self.requests.popitem(last=False)
        elif method == 'Network.loadingFailed':
            request = self.requests.pop(p.get('requestId'), {})
            self.buffer.add('request_failed', **stamp, **request,
                text=str(p.get('errorText', 'request failed'))[:1000], canceled=bool(p.get('canceled')))
        elif method == 'Network.responseReceived':
            response = p.get('response', {})
            if response.get('status', 0) >= 400:
                self.buffer.add('http_error', **stamp, url=safe_url(response.get('url', '')),
                    status=response['status'], text=str(response.get('statusText', ''))[:300])
        elif method == 'Network.loadingFinished':
            self.requests.pop(p.get('requestId'), None)
        elif method == 'Page.frameNavigated' and not p.get('frame', {}).get('parentId'):
            self.buffer.clear()
            self.requests.clear()
            self.target['url'] = safe_url(p.get('frame', {}).get('url', ''))
        elif method in ('Inspector.detached', 'Target.targetDestroyed'):
            raise ConnectionError('selected browser target detached')

    def describe(self):
        return {'adapter': 'cdp', 'target': dict(self.target), 'capabilities': list(self.capabilities),
                'unsupported': dict(self.unsupported), 'state': self.state,
                'timestamp_unit': 'unix_seconds'}

    def read(self, **kwargs):
        return {**self.describe(), **self.buffer.read(**kwargs), 'connection_error': self.error}

    def clear(self):
        return {**self.describe(), **self.buffer.clear()}

    def close(self):
        self.stop.set()
        self._wake_pending()
        if self.ws is not None:
            try:
                self.ws.close(timeout=0.5)
            finally:
                # websocket-client.close returns early after a peer close frame
                # marks connected=False, leaving its socket open. Always release
                # the transport too, including disconnected/error paths.
                self.ws.shutdown()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise RuntimeError('browser receiver cleanup unconfirmed')
        self.thread = None
        self.ws = None
        with self.pending_lock:
            self.pending.clear()
        self.buffer.clear()
        self.requests.clear()
        self.state = 'closed'
