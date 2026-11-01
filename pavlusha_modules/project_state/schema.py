"""Module-owned action descriptions; no Core action registration."""
from pavlusha_agent.extensions import Tool
from .model import WORK_STATUSES


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def declarations(invoke, *, combined_review=False):
    text = {'type': 'string', 'minLength': 1}
    strings = {'type': 'array', 'items': text, 'uniqueItems': True}
    work = obj({'objective': text, 'status': {'enum': ['PLANNED', 'ACTIVE']},
                'deliverables': strings}, ('objective',))
    design = obj({'decision': text, 'rationale': text}, ('decision', 'rationale'))
    changes = []
    def change(name, properties, required):
        shape = obj({'op': {'const': name}, **properties}, ('op', *required))
        changes.append(shape)
        return shape
    change('add_design', design['properties'], ('decision', 'rationale'))
    change('supersede_design', {'id': text, 'deviation': text}, ('id', 'deviation'))
    change('add_work', work['properties'], ('objective',))
    update = change('update_work', {'id': text, 'status': {'enum': sorted(WORK_STATUSES)},
        'evidence': strings, 'reason': text, 'deviation': text}, ('id',))
    update['minProperties'] = 3
    change('record_deviation', {'affects': {**strings, 'minItems': 1}, 'original': text,
                               'actual': text, 'reason': text}, ('affects', 'original', 'actual', 'reason'))
    shapes = {
        'project_init': obj({'design': {'type': 'array', 'items': design},
                            'work': {'type': 'array', 'items': work, 'minItems': 2}}, ('work',)),
        'project_update': obj({'changes': {'type': 'array', 'items': {'anyOf': changes},
                                         'minItems': 1, 'maxItems': 32}}, ('changes',)),
        'project_review_complete': obj({'handoff': text, 'note': {'type': 'string'}}, ('handoff',)),
        'finish': obj({'summary': text}, ('summary',)),
    }
    if combined_review:
        shapes['project_review_complete']['properties']['changes'] = {
            'type': 'array', 'items': {'anyOf': changes}, 'minItems': 1, 'maxItems': 32}
    descriptions = {'project_init': 'Initialize the durable project plan before ordinary work.',
                    'project_update': 'Atomically update durable work, decisions and evidence pointers.',
                    'project_review_complete': 'Commit Project State and a brief continuation handoff together.',
                    'finish': 'Finish only after all work is DONE or SUPERSEDED; unfinished work is rejected.'}
    return {name: Tool(name, descriptions[name], shape,
                      lambda args, context, name=name: invoke(name, args)) for name, shape in shapes.items()}
