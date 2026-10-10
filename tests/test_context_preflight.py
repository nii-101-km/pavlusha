"""Emergency HIGH regressions using real controller transitions, scripted transport."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.runtime import PromptBudget
from pavlusha_agent.state_store import StateStore
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done


INCIDENT_UPDATE = {"action": "project_update", "changes": [
    {"op": "record_deviation", "affects": ["W001"], "original": "Page image renders",
     "actual": "404 for unprocessed PDF page", "reason": "Observed in GUI"},
    {"op": "add_design", "decision": "Render page image on demand",
     "rationale": "Reuse existing ensure_page_image"},
]}
INCIDENT_TASK = "T" * 4000 + " task"
INCIDENT_OPTIONS = ['--worker-context-budget', '117248', '--max-tokens', '8000',
                    '--project-review-every', '1', '--history-high', '30']


def prompt_size_bound(messages):
    """Historical byte witness only; never imported by production code."""
    def bound(value):
        if isinstance(value, str):
            return 8192 if value.startswith('data:image/') else len(value.encode())
        if isinstance(value, list):
            return sum(map(bound, value))
        if isinstance(value, dict):
            return 8192 if value.get('type') == 'image_url' else sum(map(bound, value.values()))
        return len(str(value).encode())
    return 256 + sum(32 + sum(map(bound, m.values())) for m in messages)


def legacy_estimate(budget, messages):
    """Before-repair calculation, retained only as a regression witness."""
    value = prompt_size_bound(messages)
    if budget.measured is not None:
        common = 0
        for old, new in zip(budget.previous, messages):
            if old != new:
                break
            common += 1
        value = max(value, budget.measured + prompt_size_bound(messages[common:]) - 256)
    return value


def incident_replies(action=None, *, initial=72303):
    update = turn(action or INCIDENT_UPDATE, reasoning='U' * 12000)
    update.prompt_tokens = 22709
    return [init_turn(reasoning='P' * initial), done('verified'),
            turn({'action': 'shell', 'command': 'inspect image endpoint'}), update,
            review('Fix the confirmed page image defect.'),
            turn({'action': 'finish', 'summary': 'done'})]


def required(messages):
    return any('PROJECT CHECKPOINT REQUIRED' in m.get('content', '')
               for m in messages[1:] if isinstance(m.get('content'), str))


class PreflightTests(unittest.TestCase):
    def test_unknown_and_below_threshold_usage_do_not_trigger_high(self):
        budget = PromptBudget(117248, .85)
        self.assertFalse(budget.needs_checkpoint())
        budget.observe(31771)
        self.assertFalse(budget.needs_checkpoint())
        budget.observe(None)
        self.assertIsNone(budget.measured)

    def test_exact_measured_threshold(self):
        budget = PromptBudget(117248, .85)
        self.assertEqual(budget.high, 99660)
        budget.observe(99659)
        self.assertFalse(budget.needs_checkpoint())
        budget.observe(99660)
        self.assertTrue(budget.needs_checkpoint())

    def test_previous_112021_fixture_is_admitted_without_byte_high(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies = incident_replies()
            # A checkpoint is no longer justified at provider usage 22709.
            replies[4] = turn({'action':'finish', 'summary':'done'})
            seen, result, error = run_script(Path(tmp), replies, extra=INCIDENT_OPTIONS, task=INCIDENT_TASK)
            self.assertEqual(result, 0, error)
            self.assertFalse(required(seen[4]))
            # Preserve the exact previous numerical witness with a HIGH prefix.
            from pavlusha_agent.project_state import project_state_message
            from types import SimpleNamespace
            state = StateStore(Path(tmp)/'state', INCIDENT_TASK).load()['project_state']
            candidate = copy.deepcopy(seen[4])
            candidate[2] = project_state_message(state, review_required=(
                'context usage reached the configured HIGH fraction of usable prompt context; '
                'review durable recovery information before the full history reset'))
            self.assertEqual(legacy_estimate(SimpleNamespace(previous=seen[3], measured=22709), candidate), 112021)

    def test_step19_reasoning_shell_continuation_is_admitted(self):
        reasoning = (Path(__file__).parent/'fixtures/step19_reasoning.txt').read_text()
        replies = [init_turn(reasoning='P'*83000), done('verified')]
        replies += [turn({'action':'shell', 'command':f'inspect file_{index}'}) for index in range(15)]
        eighteenth = turn({'action':'shell', 'command':'free -h'}, reasoning='Q'*10000)
        eighteenth.prompt_tokens = 27741
        nineteenth = turn({'action':'shell', 'command':'start background server'}, reasoning=reasoning)
        nineteenth.prompt_tokens = 31771
        replies += [eighteenth, nineteenth, turn({'action':'finish', 'summary':'done'})]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, replies, extra=[
                '--worker-context-budget','117248','--max-tokens','8000','--history-high','30'])
            self.assertEqual(result, 0, error)
            self.assertEqual(len(seen), 20)
            self.assertFalse(required(seen[19]))
            self.assertGreater(prompt_size_bound(seen[19]), 109248)
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_provider_high_commits_handoff_archives_then_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell = turn({'action':'shell','command':'verify'})
            shell.prompt_tokens = 99660
            seen, result, error = run_script(root, [init_turn(), done('verified'), shell,
                review('Next: inspect current files.'), turn({'action':'finish','summary':'done'})],
                extra=['--worker-context-budget','117248','--max-tokens','8000','--history-high','30'])
            self.assertEqual(result, 0, error)
            self.assertTrue(required(seen[3]))
            self.assertIn('CHECKPOINT HANDOFF', str(seen[4]))
            self.assertFalse(any(str(m.get('content','')).startswith('SHELL RESULT') for m in seen[4]))
            archive = [json.loads(l) for l in (root/'state/history_archive.jsonl').read_text().splitlines()]
            self.assertEqual({x['step'] for x in archive}, {1,2,3,4})
            self.assertEqual(StateStore(root/'state','task').load()['counters']['operation'], 1)
