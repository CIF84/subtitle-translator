"""A production-schema probe makes one request, retains charges, and never publishes partial SRT."""
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr
from test_truncation_fix import args_for, legacy_job, success_client, sample


@pytest.fixture
def pricing():
    return tr.load_pricing(tr.BASE / 'pricing.json', 'gpt-5.4-mini')[0]


@pytest.fixture
def price(pricing):
    return pricing['models']['gpt-5.4-mini']


@pytest.fixture
def probe(tmp_path, pricing, price):
    args = args_for(tmp_path, 12)
    path, state = legacy_job(args, pricing, price)
    # Reproduce the fifth real charge without reading any real subtitle content.
    state['requests'].append({'status': 'settled', 'reservation': '0.021933', 'input_tokens': 1457,
                              'output_tokens': 3072, 'cached_tokens': 0, 'reasoning_tokens': 0,
                              'cost': '0.01491675', 'response_status': 'incomplete',
                              'incomplete_reason': 'max_output_tokens', 'batch_first_id': '1'})
    tr.save_state(path, state)
    args.resume, args.probe = True, 5
    return args, path, state


def reply(rows, *, status='completed', output=300, reasoning=0):
    return NS(status=status, incomplete_details=NS(reason='max_output_tokens') if status=='incomplete' else None,
              output_text=tr.dumps({'entries': rows, 'glossary': []}), output=[],
              usage=NS(input_tokens=500, output_tokens=output, input_tokens_details=NS(cached_tokens=0),
                       output_tokens_details=NS(reasoning_tokens=reasoning)))


def test_probe_one_request_uses_production_prompt_schema_and_context(probe, capsys):
    args, path, original = probe
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    client.models.retrieve.assert_not_called()
    client.close.assert_called_once()
    request = client.responses.create.call_args.kwargs
    payload = json.loads(request['input'])
    selected = tr.read_source(args.input)[1][:5]
    assert request['instructions'] == tr.SYSTEM_PROMPT.format(source='English', target='Czech')
    assert request['text']['format']['schema'] == tr.response_schema(selected)
    assert request['reasoning'] == {'effort': 'none'} and request['max_output_tokens'] == 4096
    assert [row['id'] for row in payload['translate']] == ['1', '2', '3', '4', '5']
    assert [row['id'] for row in payload['after']] == ['6', '7', '8', '9', '10']
    assert 'validation_feedback' not in payload and payload['established_glossary'] == []
    state = json.loads(path.read_text())
    assert state['requests'][:5] == original['requests'] and state['identity'] == original['identity']
    assert tr.totals(state)[0] == Decimal('0.06581850')
    assert set(state['translations']) == {'1', '2', '3', '4', '5'}
    assert state['requests'][-1]['mode'] == 'probe' and state['requests'][-1]['probe_entries'] == 5
    assert state['configuration_history'][-1]['config']['retries'] == 0
    assert state['configuration_history'][-1]['config']['max_output_tokens'] == 4096
    assert not args.output_dir.exists() and len(list(args.checkpoint_dir.glob('*.json'))) == 1
    output = capsys.readouterr().out
    for field in ['Response status: completed', 'Reasoning tokens: 0', 'Visible response length:',
                  'Translation entry count: 5 / 5 expected', 'Validation: PASSED',
                  'Actual probe cost: $0.000600', 'Ahoj, příteli.', 'Probe stopped.']:
        assert field in output


@pytest.mark.parametrize('answer', ['', 'n', 'no'])
def test_probe_approval_defaults_no_without_migration_or_requests(probe, answer):
    args, path, _ = probe
    before = path.read_bytes()
    forbidden = Mock(side_effect=AssertionError('No client without approval'))
    tr.run(args, client_factory=forbidden, input_fn=lambda _: answer)
    forbidden.assert_not_called()
    assert path.read_bytes() == before and not path.with_suffix('.json.bak').exists()


def test_probe_dry_run_estimates_only_the_selected_request(probe, price, capsys):
    args, path, _ = probe
    before = path.read_bytes()
    args.dry_run = True
    forbidden = Mock(side_effect=AssertionError('No client during preflight'))
    tr.run(args, client_factory=forbidden, input_fn=Mock(side_effect=AssertionError('No approval in dry run')))
    config = tr.Config(retries=0, reasoning_effort='none', max_output_tokens=4096)
    entries = tr.read_source(args.input)[1]
    expected = tr.estimate_request(entries[:5], config, tr.payload_for(entries, entries[:5], config, {}, []), price, tr.TokenCounter())
    output = capsys.readouterr().out
    assert f'Conservative additional estimate (all retries): {tr.money(expected.upper_cost)}' in output
    assert 'Previous actual spending: $0.065218' in output and 'Maximum budget: $1.000000' in output
    assert 'Output token caps: 4096–4096' in output and 'Planned remaining batches: 1' in output
    assert path.read_bytes() == before and not path.with_suffix('.json.bak').exists()
    forbidden.assert_not_called()


def test_probe_budget_counts_all_previous_charges_and_unknown_reservations(probe):
    args, path, original = probe
    state = json.loads(path.read_text())
    state['requests'].append({'status': 'unknown', 'reservation': '0.920', 'batch_first_id': '1'})
    tr.save_state(path, state)
    forbidden = Mock(side_effect=AssertionError('No affordable request'))
    with pytest.raises(tr.TranslationError, match='Budget guard'):
        tr.run(args, client_factory=forbidden, input_fn=lambda _: 'y')
    forbidden.assert_not_called()
    after = json.loads(path.read_text())
    assert after['requests'] == state['requests'] and tr.totals(after)[:2] == (Decimal('0.06521850'), Decimal('0.920'))


def test_probe_success_reused_by_normal_resume(probe):
    args, path, _ = probe
    first = success_client()
    tr.run(args, client_factory=lambda: first, input_fn=lambda _: 'y')
    historical = json.loads(path.read_text())['requests']
    args.probe = None
    second = success_client()
    tr.run(args, client_factory=lambda: second, input_fn=lambda _: 'y')
    ids = [row['id'] for call in second.responses.create.call_args_list for row in json.loads(call.kwargs['input'])['translate']]
    assert ids == [str(i) for i in range(6, 13)]
    state = json.loads(path.read_text())
    assert state['requests'][:6] == historical and tr.totals(state)[0] == Decimal('0.06641850')
    assert len(state['translations']) == 12
    assert len(tr.parse_srt(next(args.output_dir.glob('*.srt')).read_text())) == 12
    second.models.retrieve.assert_called_once()


def test_probe_selects_first_untranslated_even_across_completed_gaps(probe):
    args, path, _ = probe
    state = json.loads(path.read_text())
    state['translations'] = {'1': 'Ahoj, příteli.', '3': 'Ahoj, příteli.', '4': 'Ahoj, příteli.'}
    tr.save_state(path, state)
    args.batch_size = 1  # Probe N takes precedence over production batching.
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    selected = json.loads(client.responses.create.call_args.kwargs['input'])['translate']
    assert [row['id'] for row in selected] == ['2', '5', '6', '7', '8']
    assert json.loads(path.read_text())['translations']['1'] == state['translations']['1']


@pytest.mark.parametrize('failure', ['truncated', 'missing', 'duplicate', 'formatting', 'empty', 'refusal', 'bad_json', 'no_usage'])
def test_probe_failures_charge_one_attempt_without_retry_or_partial_output(probe, failure, capsys):
    args, path, original = probe
    args.retries = 5  # Cannot cause a second request in probe mode.
    rows = [{'id': str(i), 'text': 'Ahoj, příteli.'} for i in range(1, 6)]
    response = reply(rows, status='incomplete' if failure=='truncated' else 'completed', output=4096 if failure=='truncated' else 300)
    if failure=='missing':
        response.output_text = tr.dumps({'entries': rows[:-1], 'glossary': []})
    elif failure=='duplicate':
        response.output_text = tr.dumps({'entries': rows[:-1] + [rows[0]], 'glossary': []})
    elif failure=='formatting':
        rows[0]['text'] = '<i>Ahoj, příteli.</i>'
        response.output_text = tr.dumps({'entries': rows, 'glossary': []})
    elif failure=='empty':
        rows[0]['text'] = ''
        response.output_text = tr.dumps({'entries': rows, 'glossary': []})
    elif failure=='refusal':
        response.output = [NS(content=[NS(type='refusal', refusal='PRIVATE REFUSAL')])]
    elif failure=='bad_json':
        response.output_text = 'PRIVATE BAD RESPONSE'
    elif failure=='no_usage':
        response.usage = None
    client = Mock()
    client.responses.create.return_value = response
    with pytest.raises(tr.TranslationError):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y', sleep=Mock(side_effect=AssertionError('No retry')))
    client.responses.create.assert_called_once()
    client.models.retrieve.assert_not_called()
    client.close.assert_called_once()
    state = json.loads(path.read_text())
    assert state['requests'][:5] == original['requests'] and state['translations'] == {}
    last = state['requests'][-1]
    assert last['mode'] == 'probe'
    assert last['visible_response_chars'] == len(response.output_text)
    if failure=='no_usage':
        assert last['status'] != 'settled' and tr.totals(state)[0] == Decimal('0.06521850')
        assert tr.totals(state)[1] > 0
    else:
        assert tr.totals(state)[0] == Decimal('0.06521850') + tr.cost(tr.load_pricing(args.pricing, args.model)[1], 500, response.usage.output_tokens)
    assert not args.output_dir.exists()
    output = capsys.readouterr().out
    assert 'PROBE RESULT' in output and 'Validation: FAILED' in output and 'Translation entry count:' in output
    assert 'PRIVATE REFUSAL' not in output and 'PRIVATE BAD RESPONSE' not in output
    assert 'Ahoj, příteli.' not in output


@pytest.mark.parametrize('exception', [TimeoutError('PRIVATE KEY'), ConnectionError('PRIVATE KEY'), KeyboardInterrupt()])
def test_probe_network_failure_or_interrupt_leaves_reservation_without_retry(probe, exception, capsys):
    args, path, original = probe
    args.retries = 5
    client = Mock()
    client.responses.create.side_effect = exception
    with pytest.raises((tr.TranslationError, KeyboardInterrupt)):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y', sleep=Mock(side_effect=AssertionError('No retry')))
    client.responses.create.assert_called_once()
    client.models.retrieve.assert_not_called()
    client.close.assert_called_once()
    state = json.loads(path.read_text())
    assert len(state['requests']) == 6 and state['requests'][:5] == original['requests']
    assert tr.totals(state)[0] == Decimal('0.06521850') and tr.totals(state)[1] > 0
    assert state['translations'] == {} and not args.output_dir.exists()
    output = capsys.readouterr().out
    assert 'PROBE RESULT' in output and 'Actual probe cost: unknown; reservation retained' in output
    assert 'PRIVATE KEY' not in output and 'PRIVATE KEY' not in path.read_text()


def test_probe_distinguishes_reasoning_from_visible_output(probe, capsys):
    args, path, _ = probe
    args.reasoning_effort, args.max_output_tokens = 'low', 6144
    response = reply([{'id': str(i), 'text': 'Ahoj, příteli.'} for i in range(1, 6)], output=900, reasoning=600)
    client = Mock()
    client.responses.create.return_value = response
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    output = capsys.readouterr().out
    assert 'Output tokens: 900' in output and 'Reasoning tokens: 600' in output
    assert 'Non-reasoning output tokens (includes formatting): 300' in output
    assert f'Visible response length: {len(response.output_text)} characters' in output
    assert 'Visible text token estimate:' in output
    assert client.responses.create.call_args.kwargs['max_output_tokens'] == 6144


def test_probe_no_remaining_entries_makes_zero_requests_and_does_not_republish(probe):
    args, path, _ = probe
    state = json.loads(path.read_text())
    state['translations'] = {str(i): 'Ahoj, příteli.' for i in range(1, 13)}
    tr.save_state(path, state)
    before = path.read_bytes()
    forbidden = Mock(side_effect=AssertionError('Nothing to probe'))
    tr.run(args, client_factory=forbidden, input_fn=Mock(side_effect=AssertionError('No spending approval necessary')))
    forbidden.assert_not_called()
    assert path.read_bytes() == before and not args.output_dir.exists()


def test_probe_fewer_entries_remaining_uses_one_smaller_batch(probe):
    args, path, _ = probe
    state = json.loads(path.read_text())
    state['translations'] = {str(i): 'Ahoj, příteli.' for i in range(1, 11)}
    tr.save_state(path, state)
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    assert [row['id'] for row in json.loads(client.responses.create.call_args.kwargs['input'])['translate']] == ['11', '12']
    client.responses.create.assert_called_once()
    assert not args.output_dir.exists()


@pytest.mark.parametrize('count', [0, -1, 501])
def test_invalid_probe_count_fails_locally(probe, count):
    args, path, _ = probe
    before = path.read_bytes()
    args.probe = count
    forbidden = Mock(side_effect=AssertionError('Invalid count'))
    with pytest.raises(tr.TranslationError, match='--probe'):
        tr.run(args, client_factory=forbidden)
    forbidden.assert_not_called()
    assert path.read_bytes() == before


def test_oversized_probe_fails_without_splitting_or_spending(probe):
    args, path, _ = probe
    args.probe, args.max_output_tokens = 12, 3072
    # Make the exact single probe too large; it must not become multiple requests.
    args.input.write_text(sample(12, 'A long line of dialogue. ' * 100))
    before = path.read_bytes()
    args.resume = False
    forbidden = Mock(side_effect=AssertionError('Probe cannot fit'))
    with pytest.raises(tr.TranslationError, match='Output-token limit'):
        tr.run(args, client_factory=forbidden)
    forbidden.assert_not_called()
    assert path.read_bytes() == before and len(list(args.checkpoint_dir.glob('*.json'))) == 1


def test_probe_without_resume_refuses_to_start_independent_budget(probe):
    args, path, _ = probe
    before = path.read_bytes()
    args.resume = False
    with pytest.raises(tr.TranslationError, match='--resume'):
        tr.run(args, client_factory=Mock(side_effect=AssertionError()))
    assert path.read_bytes() == before


def test_probe_preserves_existing_final_output(probe):
    args, path, _ = probe
    args.output_dir.mkdir()
    target = args.output_dir / (args.input.stem + '.cs.srt')
    target.write_text('existing completed output')
    tr.run(args, client_factory=success_client, input_fn=lambda _: 'y')
    assert target.read_text() == 'existing completed output'


def test_probe_terminal_display_escapes_controls_without_changing_checkpoint(probe, capsys):
    args, path, _ = probe
    client = Mock()
    rows = [{'id': str(i), 'text': 'Ahoj\x1b[31m' if i==1 else 'Ahoj, příteli.'} for i in range(1, 6)]
    # Brackets are protected production markers, so use an ESC sequence without brackets.
    rows[0]['text'] = 'Ahoj\x1bP'
    client.responses.create.return_value = reply(rows)
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    output = capsys.readouterr().out
    assert '\x1b' not in output and 'Ahoj\\u001bP' in output
    assert json.loads(path.read_text())['translations']['1'] == 'Ahoj\x1bP'


def test_probe_invalid_unicode_cannot_hide_returned_usage(probe):
    args, path, _ = probe
    client = Mock()
    response = reply([])
    response.output_text = '\ud800'
    client.responses.create.return_value = response
    with pytest.raises(tr.TranslationError):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    state = json.loads(path.read_text())
    assert state['requests'][-1]['status'] == 'settled'
    assert tr.totals(state)[0] == Decimal('0.06694350')
    client.responses.create.assert_called_once()


def test_probe_one_request_in_batch_engine_even_with_retry_enabled(tmp_path, pricing, price):
    entries = tr.parse_srt(sample(5))
    config = tr.Config(retries=5, max_output_tokens=4096, reasoning_effort='none')
    identity, _ = tr.identity_for(sample(5).encode(), config, pricing)
    state = tr.new_state(identity)
    client = Mock()
    client.responses.create.side_effect = TimeoutError('PRIVATE')
    with pytest.raises(tr.TranslationError):
        tr.translate_batch(client, entries, entries, config, price, tr.TokenCounter(), state,
                           tmp_path / 'checkpoint.json', Decimal('1.00'),
                           sleep=Mock(side_effect=AssertionError('No retry')), probe=True)
    client.responses.create.assert_called_once()
    assert len(state['requests']) == 1 and tr.totals(state)[1] > 0


def test_probe_final_checkpoint_write_failure_reports_no_confirmed_save(probe, monkeypatch, capsys):
    args, path, _ = probe
    save = tr.save_state
    def fail_final(path, state):
        if state['requests'] and state['requests'][-1].get('validation'):
            raise OSError('PRIVATE DISK ERROR')
        save(path, state)
    monkeypatch.setattr(tr, 'save_state', fail_final)
    client = success_client()
    with pytest.raises(OSError):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    client.close.assert_called_once()
    state = json.loads(path.read_text())
    assert state['translations'] == {} and tr.totals(state)[0] == Decimal('0.06581850')
    output = capsys.readouterr().out
    assert 'checkpoint write not confirmed' in output and 'saved to checkpoint for normal resumption' not in output
    assert 'PRIVATE DISK ERROR' not in output and not args.output_dir.exists()
