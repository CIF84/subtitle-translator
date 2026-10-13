"""Forensic capture is local, opt-in, private and independent of strict acceptance."""
from dataclasses import replace
from decimal import Decimal
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr
from test_truncation_fix import args_for, legacy_job, success_client


@pytest.fixture
def job(tmp_path):
    args = args_for(tmp_path, 12)
    pricing, price = tr.load_pricing(args.pricing, args.model)
    path, state = legacy_job(args, pricing, price)
    state['identity']['prompt_version'] = '1.0'
    state['identity']['prompt_sha256'] = tr.LEGACY_PROMPT_SHA256
    for inp, out in [(1457, 3072), (601, 96), (680, 4096)]:
        state['requests'].append({'status': 'settled', 'reservation': '0.02192475',
                                 'input_tokens': inp, 'output_tokens': out, 'cached_tokens': 0,
                                 'cost': str(tr.cost(price, inp, out))})
    tr.save_state(path, state)
    assert tr.totals(state)[0] == Decimal('0.08504325')
    args.resume, args.probe, args.debug_responses = True, 5, True
    args.response_schema = 'constrained'  # Historical v3 capture remains supported.
    return args, path, state, price


def reply(raw, status='completed', usage=True):
    return NS(status=status, incomplete_details=NS(reason='max_output_tokens') if status=='incomplete' else None,
              output_text=raw, output=[], id='resp_test', _request_id='req_test',
              usage=NS(input_tokens=680, output_tokens=4096 if status=='incomplete' else 96,
                       input_tokens_details=NS(cached_tokens=0),
                       output_tokens_details=NS(reasoning_tokens=0)) if usage else None)


def failed_run(job, response):
    args, path, original, price = job
    client = Mock()
    client.responses.create.return_value = response
    with pytest.raises(tr.TranslationError):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    client.models.retrieve.assert_not_called()
    state = json.loads(path.read_text())
    assert state['requests'][:7] == original['requests'] and state['identity'] == original['identity']
    assert not state['translations'] and not args.output_dir.exists()
    captures = list((path.parent.parent/'debug_responses').glob('*.json'))
    return client, state, captures


def test_truncated_repetitive_text_captured_in_full_without_extra_calls(job, capsys):
    raw = ('{"entries":[' + '{"id":"1","text":"Ahoj."},'*400)[:8216]
    client, state, captures = failed_run(job, reply(raw, 'incomplete'))
    assert len(captures) == 1
    saved = json.loads(captures[0].read_text())
    assert saved['raw_generated_text'] == raw and len(raw) == 8216
    assert saved['response_metadata']['response_status'] == 'incomplete'
    assert saved['response_metadata']['incomplete_reason'] == 'max_output_tokens'
    assert saved['usage'] == {'input_tokens': 680, 'output_tokens': 4096, 'cached_tokens': 0, 'reasoning_tokens': 0}
    assert saved['output_limit'] == 4096 and saved['request_schema_version'] == 3
    assert saved['validation_failure_category'] == 'output_truncated'
    assert saved['cost'] == '0.018942'
    assert tr.totals(state)[0] == Decimal('0.10398525')
    assert os.stat(captures[0]).st_mode & 0o777 == 0o600
    assert os.stat(captures[0].parent).st_mode & 0o777 == 0o700
    assert raw not in capsys.readouterr().out and raw not in tr.dumps(state)


@pytest.mark.parametrize('raw,category', [
    ('not JSON', 'invalid_json'),
    pytest.param('['*5000 + ']'*5000, ('invalid_json', 'schema_mismatch'), id='excessive-json-depth'),
    ('{"entries":[],"entries":[],"glossary":[]}', 'duplicate_json_field'),
    ('{"translations":[]}', 'schema_mismatch'),
    ('{"entries":[],"glossary":[],"children":{"children":[]}}', 'schema_mismatch'),
    (tr.dumps({'entries': [{'id': '1', 'text': 'Ahoj.'}]*5, 'glossary': []}), 'identifiers'),
    ('{"entries":[],"glossary":[]}'*5, 'invalid_json'),
])
def test_invalid_and_repetitive_structured_responses_remain_rejected_and_captured(job, raw, category):
    _, state, captures = failed_run(job, reply(raw))
    assert json.loads(captures[0].read_text())['raw_generated_text'] == raw
    assert state['requests'][-1]['diagnostic']['category'] in ((category,) if isinstance(category, str) else category)
    assert tr.totals(state)[0] == Decimal('0.08598525')


def test_repetitive_overlong_translation_rejected_under_simple_schema(job):
    args, *_ = job
    args.response_schema = 'simple'
    raw = tr.dumps({'entries': [{'id': str(i), 'text': 'Ahoj. '*300 if i==1 else 'Ahoj.'} for i in range(1,6)], 'glossary': []})
    client, state, captures = failed_run(job, reply(raw))
    saved = json.loads(captures[0].read_text())
    assert saved['validation_failure_category'] == 'invalid_text'
    assert saved['request_schema_version'] == 4 and saved['request_schema_mode'] == 'simple'
    assert 'anyOf' not in tr.dumps(client.responses.create.call_args.kwargs['text'])


def test_capture_excludes_sdk_headers_objects_and_redacts_generated_credentials(job, monkeypatch, capsys):
    args, path, _, _ = job
    monkeypatch.setenv('TEST_ACCESS_TOKEN', 'private-env-token-123')
    client = Mock(api_key='private-client-key-123')
    raw = 'private-client-key-123 private-env-token-123 sk-proj-hallucinated123 Bearer unknown-token-456 "password":"hidden-pass"'
    response = reply(raw)
    response.headers = {'Authorization': 'header-only-secret'}
    response.error = 'error-only-secret'
    response.metadata = {'credential': 'metadata-only-secret'}
    response.output = [NS(type='message', content=[NS(type='output_text', text=raw), NS(type='refusal', refusal='refusal-only-secret')])]
    response.model_dump = Mock(side_effect=AssertionError('Never serialize whole response'))
    client.responses.create.return_value = response
    with pytest.raises(tr.InvalidBatch):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    response.model_dump.assert_not_called()
    saved_file = next((path.parent.parent/'debug_responses').glob('*.json'))
    saved = saved_file.read_text()
    terminal_output = capsys.readouterr().out
    for secret in ['private-client-key-123', 'private-env-token-123', 'sk-proj-hallucinated123',
                   'unknown-token-456', 'hidden-pass', 'header-only-secret', 'error-only-secret',
                   'metadata-only-secret', 'refusal-only-secret']:
        assert secret not in saved and secret not in path.read_text() and secret not in terminal_output
    document = json.loads(saved)
    assert '[REDACTED]' in document['raw_generated_text']
    assert document['output_text_parts'][0]['message_index'] == 0
    assert document['output_text_parts'][0]['part_index'] == 0
    assert document['output_text_parts'][0]['text'] == document['raw_generated_text']


def test_capture_disabled_by_default(job):
    job[0].debug_responses = False
    _, _, captures = failed_run(job, reply('BAD PRIVATE CONTENT'))
    assert captures == []


@pytest.mark.parametrize('action', ['dry_run', 'decline'])
def test_debug_simple_schema_preflight_never_mutates_checkpoint_or_calls_api(job, action):
    args, path, _, _ = job
    before = path.read_bytes()
    args.response_schema, args.dry_run = 'simple', action=='dry_run'
    forbidden = Mock(side_effect=AssertionError('No API allowed'))
    tr.run(args, client_factory=forbidden, input_fn=lambda _: 'n')
    forbidden.assert_not_called()
    assert path.read_bytes() == before and not (path.parent.parent/'debug_responses').exists()


def test_success_never_saved_to_failed_response_directory(job):
    args, path, _, _ = job
    args.response_schema = 'simple'
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    assert not (path.parent.parent/'debug_responses').exists()
    state = json.loads(path.read_text())
    assert len(state['translations']) == 5
    assert state['configuration_history'][-1]['config']['response_schema'] == 'simple'
    assert state['requests'][-1]['response_schema_version'] == 4
    assert tr.totals(state)[0] == Decimal('0.08564325')


def test_capture_unavailable_usage_preserves_reservation(job):
    _, state, captures = failed_run(job, reply('{"entries":[', 'incomplete', usage=False))
    saved = json.loads(captures[0].read_text())
    assert saved['validation_failure_category'] == 'usage_unavailable'
    assert saved['usage']['output_tokens'] is None and saved['cost'] is None
    assert tr.totals(state)[0] == Decimal('0.08504325') and tr.totals(state)[1] > 0


def test_api_error_capture_has_no_response_body_or_sensitive_exception(job, capsys):
    args, path, _, _ = job
    client = Mock()
    client.responses.create.side_effect = RuntimeError('Authorization: Bearer PRIVATE_EXCEPTION_TOKEN')
    with pytest.raises(tr.TranslationError):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    saved_file = next((path.parent.parent/'debug_responses').glob('*.json'))
    saved = json.loads(saved_file.read_text())
    assert saved['raw_generated_text'] is None
    assert saved['response_metadata']['response_status'] == 'not_received'
    assert saved['validation_failure_category'] == 'api_request'
    assert 'PRIVATE_EXCEPTION_TOKEN' not in saved_file.read_text() + path.read_text() + capsys.readouterr().out
    assert tr.totals(json.loads(path.read_text()))[0] == Decimal('0.08504325')
    assert tr.totals(json.loads(path.read_text()))[1] > 0


def test_invalid_unicode_failure_can_be_captured_without_hiding_returned_usage(job):
    _, state, captures = failed_run(job, reply('\ud800'))
    assert json.loads(captures[0].read_text())['raw_generated_text'] == '\ud800'
    assert state['requests'][-1]['status'] == 'settled'


def test_capture_disk_error_never_hides_failure_or_causes_another_request(job, monkeypatch, capsys):
    original = tr.atomic_write
    def write(path, text):
        if path.parent.name == 'debug_responses':
            raise OSError('PRIVATE LOCAL ERROR')
        return original(path, text)
    monkeypatch.setattr(tr, 'atomic_write', write)
    _, state, captures = failed_run(job, reply('not JSON'))
    assert captures == [] and tr.totals(state)[0] == Decimal('0.08598525')
    output = capsys.readouterr().out
    assert 'could not be written' in output and 'PRIVATE LOCAL ERROR' not in output


def test_multiple_failed_requests_do_not_overwrite_captures(job):
    failed_run(job, reply('first private failure'))
    args, path, _, _ = job
    client = Mock()
    client.responses.create.return_value = reply('second private failure')
    with pytest.raises(tr.InvalidBatch):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    captures = list((path.parent.parent/'debug_responses').glob('*.json'))
    assert len(captures) == 2
    assert {json.loads(p.read_text())['raw_generated_text'] for p in captures} == {'first private failure', 'second private failure'}


def test_simple_schema_is_smaller_nonrecursive_and_has_no_regex(job):
    args, _, _, price = job
    entries = tr.read_source(args.input)[1][:5]
    simple, constrained = tr.response_schema(entries, 'simple'), tr.response_schema(entries, 'constrained')
    serialized = tr.dumps(simple)
    assert not any(key in serialized for key in ['anyOf', 'pattern', '$ref', '$defs'])
    assert len(serialized) < len(tr.dumps(constrained))
    array = simple['properties']['entries']
    assert array['minItems'] == array['maxItems'] == 5
    assert array['items']['properties']['id']['enum'] == ['1','2','3','4','5']
    assert simple['additionalProperties'] is False
    baseline = tr.Config(max_output_tokens=4096, reasoning_effort='none', response_schema='constrained')
    changed = replace(baseline, response_schema='simple')
    payload = tr.payload_for(entries, entries, baseline, {}, [])
    old_request = tr.request_for(entries, baseline, payload, price, 4096)
    new_request = tr.request_for(entries, changed, payload, price, 4096)
    assert {k:v for k,v in old_request.items() if k!='text'} == {k:v for k,v in new_request.items() if k!='text'}
    a = tr.estimate_request(entries, baseline, payload, price, tr.TokenCounter())
    b = tr.estimate_request(entries, changed, payload, price, tr.TokenCounter())
    assert b.upper_input < a.upper_input and b.upper_cost < a.upper_cost


def test_diagnostics_are_ignored_by_version_control():
    assert 'debug_responses/' in (tr.BASE/'.gitignore').read_text().splitlines()
