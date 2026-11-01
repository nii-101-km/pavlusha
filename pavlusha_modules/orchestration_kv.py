"""Save and restore a llama.cpp slot without stopping the model server."""
from __future__ import annotations

import json
from pathlib import Path
import urllib.request
from uuid import uuid4

from pavlusha_agent.core import AgentError


class SlotSnapshot:
    def __init__(self, base_url, storage_dir, timeout=300):
        self.endpoint = base_url.rstrip('/').removesuffix('/v1')
        self.storage_dir = Path(storage_dir)
        self.timeout = timeout
        self.snapshot = None

    def _request(self, route, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {} if data is None else {'Content-Type': 'application/json'}
        request = urllib.request.Request(self.endpoint + route, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def save(self):
        if self.snapshot is not None:
            raise AgentError('KV snapshot already exists')
        filename = 'orchestration-' + uuid4().hex + '.bin'
        path = self.storage_dir / filename
        try:
            slots = self._request('/slots')
            if (not isinstance(slots, list) or len(slots) != 1 or not isinstance(slots[0], dict)
                    or slots[0].get('is_processing') is not False
                    or type(slots[0].get('id')) is not int or slots[0]['id'] < 0):
                raise ValueError('expected one idle slot')
            slot = slots[0]['id']
            reply = self._request(f'/slots/{slot}?action=save', {'filename': filename})
            count, size = reply.get('n_saved'), reply.get('n_written')
            if (reply.get('id_slot') != slot or reply.get('filename') != filename
                    or type(count) is not int or count < 0 or type(size) is not int or size <= 0
                    or not path.is_file() or path.stat().st_size != size):
                raise ValueError('incomplete KV snapshot')
            self.snapshot = slot, filename, count, size
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            path.unlink(missing_ok=True)
            raise AgentError(f'KV save failed: {exc}') from exc

    def restore(self):
        if self.snapshot is None:
            raise AgentError('no KV snapshot to restore')
        slot, filename, count, size = self.snapshot
        try:
            reply = self._request(f'/slots/{slot}?action=restore', {'filename': filename})
            if (reply.get('id_slot') != slot or reply.get('filename') != filename
                    or type(reply.get('n_restored')) is not int or reply['n_restored'] != count
                    or type(reply.get('n_read')) is not int or reply['n_read'] != size):
                raise ValueError('incomplete KV restore')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise AgentError(f'KV restore failed: {exc}') from exc
        finally:
            self.discard()

    def discard(self):
        if self.snapshot is not None:
            (self.storage_dir / self.snapshot[1]).unlink(missing_ok=True)
            self.snapshot = None
