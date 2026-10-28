"""Small helpers for statically declared, trusted module tools."""
from pavlusha_agent.extensions import Tool


def declare(name, description, properties, required, invoke, *, execution=None):
    arguments = {'type': 'object', 'properties': properties, 'required': list(required),
                 'additionalProperties': False}
    return Tool(name, description, arguments, invoke, **({'execution': execution} if execution else {}))


TEXT = {'type': 'string', 'minLength': 1}
STRING = {'type': 'string'}
BOOL = {'type': 'boolean'}
NUMBER = {'type': 'number'}
INTEGER = {'type': 'integer'}
