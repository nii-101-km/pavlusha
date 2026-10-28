"""Image inspection as an event module, without private global slots."""
import base64
import io
from pavlusha_agent.extensions import Contribution, Outcome, Observation
from .bundled._images import read_image, validate_view_image_action
from .util import declare, TEXT


class Images:
    name = 'images'
    def __init__(self, root): self.root = root
    def on_event(self, event):
        if event.kind == 'session_started':
            return Contribution(message='Use view_image to inspect a file; visual observations are transient.',
                tools=(declare('view_image', 'Inspect a PNG, JPEG or WebP in /work.',
                               {'path': TEXT}, ('path',), self.view),))
    def view(self, args, context):
        validate_view_image_action({'action':'view_image', **args})
        metadata, block = read_image(args['path'], self.root)
        if block is None: return Outcome(metadata)
        url = block['image_url']['url']
        if metadata['format'] == 'WEBP':
            from PIL import Image
            raw = base64.b64decode(url.split(',', 1)[1])
            output = io.BytesIO()
            with Image.open(io.BytesIO(raw)) as image: image.save(output, format='PNG')
            url = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
        return Outcome(metadata, observations=(Observation(text='Image: ' + metadata['path'], image=url),))


def create_extension(*, settings, context):
    if settings: raise ValueError('Images accepts no settings')
    return Images(context['workdir'])
