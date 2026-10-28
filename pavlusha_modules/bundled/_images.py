"""Read-only image observations, independent of GUI and optional function packs."""
import base64
import hashlib
import io
import stat
from pathlib import Path

from pavlusha_runtime.errors import AgentError

MAX_BYTES = 8 * 1024 * 1024
MAX_SIDE = 4096
MIME_TYPES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}


def validate_view_image_action(action):
    if (set(action) != {'action', 'path'} or not isinstance(action.get('path'), str)
            or not action['path'].strip() or len(action['path']) > 4096 or '\x00' in action['path']):
        raise AgentError('view_image requires exactly action and a nonblank path (at most4096 characters)')
    return 'view_image', {'path': action['path']}


def read_image(path, workdir):
    """Return compact metadata and an image block; errors are factual observations."""
    try:
        root = Path(workdir).resolve()
        requested = Path(path)
        if requested == Path('/work') or requested.is_relative_to('/work'):
            requested = root / requested.relative_to('/work')
        resolved = (root / requested).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError('image path escapes workdir')
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError('image must be a regular file')
        with resolved.open('rb') as handle:
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('image exceeds8MiB limit')
        # Pillow is already a declared runtime dependency, imported only on use.
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image_format, mode = image.format, image.mode
            width, height = image.size
            if image_format not in MIME_TYPES:
                raise ValueError('supported formats: PNG, JPEG, WebP')
            if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
                raise ValueError('image dimensions exceed4096px limit')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('animated images are unsupported')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            alpha = image.convert('RGBA').getchannel('A')
            histogram = alpha.histogram()
            alpha_min, alpha_max = alpha.getextrema()
        metadata = {'status': 'available', 'path': str(resolved.relative_to(root)),
                    'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                    'format': image_format, 'mime_type': MIME_TYPES[image_format],
                    'width': width, 'height': height, 'mode': mode,
                    'alpha_min': alpha_min, 'alpha_max': alpha_max,
                    'transparent_pixels': histogram[0]}
        block = {'type': 'image_url', 'image_url': {
            'url': 'data:' + MIME_TYPES[image_format] + ';base64,' + base64.b64encode(data).decode('ascii')}}
        return metadata, block
    except (Exception, SystemExit) as exc:
        return {'status': 'unavailable', 'path': path, 'error': 'image_read_error',
                'detail': (type(exc).__name__ + ': ' + str(exc))[:600]}, None
