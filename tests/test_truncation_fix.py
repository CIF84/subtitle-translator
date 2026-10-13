"""Offline regressions for the confirmed 5378-token truncation and safe rebatching."""
from dataclasses import asdict, replace
from decimal import Decimal
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr


@pytest.fixture
def pricing():
    return tr.load_pricing(tr.BASE / 'pricing.json', 'gpt-5.4-mini')[0]


@pytest.fixture
def price(pricing):
    return pricing['models']['gpt-5.4-mini']


def sample(count=78, text='Hello, my friend.'):
    return '\n\n'.join(f'{i}\n00:00:01,000 --> 00:00:02,000\n{text}' for i in range(1, count + 1)) + '\n\n'


def args_for(tmp_path, count=78):
    source = tmp_path / 'test.en.srt'
    source.write_text(sample(count), encoding='utf-8')
    return tr.parser().parse_args([str(source), '--checkpoint-dir', str(tmp_path / 'cp'),
                                  '--output-dir', str(tmp_path / 'out')])


def legacy_job(args, pricing, price, *, completed=0, unknown=False):
    raw, entries = tr.read_source(args.input)
    original, _ = tr.identity_for(raw, tr.Config(batch_size=75), pricing)
    # Authentic pre-fix identity: no new operational fields.
    original['config'] = {k: original['config'][k] for k in
                          ('source', 'target', 'model', 'batch_size', 'context', 'retries')}
    state = tr.new_state(original)
    for inp, out, cached in [(1827, 1523, 0), (1827, 1613, 1280),
                             (1827, 1558, 1280), (3460, 5378, 0)]:
        state['requests'].append({'status': 'settled', 'reservation': '0.0429585',
                                 'input_tokens': inp, 'output_tokens': out, 'cached_tokens': cached,
                                 'cost': str(tr.cost(price, inp, out, cached)), 'batch_first_id': '1'})
    state['requests'][-1].update(response_status='incomplete', incomplete_reason='max_output_tokens',
                                 reasoning_tokens=0, max_output_tokens=5378)
    state['translations'] = {e.id: 'Ahoj, příteli.' for e in entries[:completed]}
    if unknown:
        state['requests'].append({'status': 'pending', 'reservation': '0.025', 'batch_first_id': '76'})
    path = args.checkpoint_dir / (tr.sha(tr.dumps(original).encode()) + '.json')
    tr.save_state(path, state)
    return path, state


def success_client():
    client = Mock()
    def create(**request):
        rows = json.loads(request['input'])['translate']
        return NS(status='completed', output=[], output_text=tr.dumps({
            'entries': [{'id': row['id'], 'text': 'Ahoj, příteli.'} for row in rows], 'glossary': []}),
            usage=NS(input_tokens=200, output_tokens=100, input_tokens_details=NS(cached_tokens=0),
                     output_tokens_details=NS(reasoning_tokens=0)))
    client.responses.create.side_effect = create
    return client


def test_default_816_entries_are_33_small_batches(price):
    config = tr.Config()
    assert config.batch_size == tr.parser().parse_args(['test.srt']).batch_size == 25
    entries = tr.parse_srt(sample(816))
    plan = tr.planned_batches(entries, config, price, tr.TokenCounter(), {})
    assert [len(batch) for batch in plan] == [25] * 32 + [16]
    assert [e for batch in plan for e in batch] == entries
    assert all(tr.output_allocation(batch, config, price, tr.TokenCounter())[1] < 5378 for batch in plan)


def test_long_source_splits_by_output_allowance_and_retains_context(price):
    entries = tr.parse_srt(sample(13, 'Long dialogue with many words. ' * 40))
    cfg, counter = tr.Config(batch_size=25, context=2), tr.TokenCounter()
    plan = tr.planned_batches(entries, cfg, price, counter, {})
    assert len(plan) > 1 and [e for batch in plan for e in batch] == entries
    assert all(tr.output_allocation(batch, cfg, price, counter)[1] <= 6144 for batch in plan)
    payload = tr.payload_for(entries, plan[1], cfg, {}, [])
    assert payload['before'] and payload['after']
    assert not ({row['id'] for row in payload['before'] + payload['after']} & {e.id for e in plan[1]})


def test_batch_size_remains_configurable(price):
    entries, counter = tr.parse_srt(sample(60)), tr.TokenCounter()
    assert len(tr.planned_batches(entries, tr.Config(batch_size=10), price, counter, {})) == 6
    assert len(tr.planned_batches(entries, tr.Config(batch_size=50), price, counter, {})) == 2


def test_token_budget_covers_json_expansion_glossary_and_reasoning(price):
    entries = tr.parse_srt(sample(25, '<i>He said "Hello!"</i>\n- Come with me.'))
    counter = tr.TokenCounter()
    cfg = tr.Config(reasoning_effort='none')
    normal = tr.estimate_request(entries, cfg, {}, price, counter)
    low = tr.estimate_request(entries, replace(cfg, reasoning_effort='low'), {}, price, counter)
    assert normal.output_cap >= normal.output_tokens
    assert low.output_tokens == normal.output_tokens + 1024
    assert low.output_cap == normal.output_cap + 2048
    assert low.expected_cost > normal.expected_cost and low.upper_cost > normal.upper_cost
    assert tr.request_for(entries, cfg, {}, price, normal.output_cap)['reasoning'] == {'effort': 'none'}
    assert tr.request_for(entries, replace(cfg, reasoning_effort='low'), {}, price, low.output_cap)['reasoning'] == {'effort': 'low'}


def test_explicit_output_cap_splits_large_batches(price):
    entries = tr.parse_srt(sample(40, 'Longer dialogue. ' * 15))
    cfg, counter = tr.Config(max_output_tokens=4096), tr.TokenCounter()
    plan = tr.planned_batches(entries, cfg, price, counter, {})
    assert len(plan) > 2
    for batch in plan:
        estimate = tr.estimate_request(batch, cfg, {}, price, counter)
        assert estimate.output_cap == 4096 and estimate.output_tokens < 4096


def test_single_entry_too_long_for_explicit_limit_fails_closed(price):
    entries = tr.parse_srt(sample(1, 'Very long dialogue. ' * 400))
    with pytest.raises(tr.TranslationError, match='Entry 1 needs a larger'):
        tr.planned_batches(entries, tr.Config(max_output_tokens=4096), price, tr.TokenCounter(), {})
    with pytest.raises(tr.TranslationError, match='model limit'):
        tr.estimate_request(entries, tr.Config(), {}, {**price, 'max_output_tokens': 1000}, tr.TokenCounter())


@pytest.mark.parametrize('setting', ['minimal', 'medium', 'high'])
def test_unverified_reasoning_overrides_fail_closed(price, setting):
    with pytest.raises(tr.TranslationError, match='Unsupported reasoning'):
        tr.request_for(tr.parse_srt(sample(1)), tr.Config(reasoning_effort=setting), {}, price, 4096)
    with pytest.raises(tr.TranslationError, match='not verified'):
        tr.effective_reasoning(tr.Config(model='custom', reasoning_effort='low'), price)


@pytest.mark.parametrize('field,value', [('reasoning_effort', 'low'), ('max_output_tokens', 4096),
                                        ('batch_size', 10), ('output_policy', 'next')])
def test_request_configuration_changes_identity_but_keeps_ledger_family(pricing, field, value):
    cfg = tr.Config(reasoning_effort='none')
    original, key = tr.identity_for(b'content', cfg, pricing)
    changed, new_key = tr.identity_for(b'content', replace(cfg, **{field: value}), pricing)
    assert key != new_key and tr.job_family(original) == tr.job_family(changed)


def test_legacy_resume_dry_run_preserves_exact_cost_and_bytes(tmp_path, pricing, price, capsys):
    args = args_for(tmp_path, 816)
    path, state = legacy_job(args, pricing, price)
    before = path.read_bytes()
    args.resume = args.dry_run = True
    forbidden = Mock(side_effect=AssertionError('No client allowed'))
    assert tr.run(args, client_factory=forbidden) == 0
    assert path.read_bytes() == before and not path.with_suffix('.json.bak').exists()
    assert tr.totals(state) == (Decimal('0.05030175'), Decimal(0), 8941, 10072, 2560)
    output = capsys.readouterr().out
    assert 'Previous actual spending: $0.050302' in output and 'Planned remaining batches: 33' in output
    assert 'Maximum budget: $1.000000' in output and 'Reasoning effort: none' in output
    forbidden.assert_not_called()


def test_resume_rebatches_remaining_ids_and_preserves_original_ledger(tmp_path, pricing, price):
    args = args_for(tmp_path, 100)
    path, original = legacy_job(args, pricing, price, completed=75, unknown=True)
    before = path.read_bytes()
    args.resume = True
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    state = json.loads(path.read_text())
    assert state['identity'] == original['identity']
    assert state['translations'].keys() == {str(i) for i in range(1, 101)}
    assert {k: state['translations'][k] for k in original['translations']} == original['translations']
    assert state['requests'][:4] == original['requests'][:4]
    assert state['requests'][4]['status'] == 'unknown' and state['requests'][4]['reservation'] == '0.025'
    assert tr.totals(state)[:2] == (Decimal('0.05090175'), Decimal('0.025'))
    assert len(list(args.checkpoint_dir.glob('*.json'))) == 1
    assert path.with_suffix('.json.bak').read_bytes() == before
    request = client.responses.create.call_args.kwargs
    assert [row['id'] for row in json.loads(request['input'])['translate']] == [str(i) for i in range(76, 101)]
    assert state['configuration_history'][-1]['config']['batch_size'] == 25
    assert state['configuration_history'][-1]['config']['reasoning_effort'] == 'none'
    assert state['requests'][-1]['reasoning_effort'] == 'none'
    assert [e.id for e in tr.parse_srt(next(args.output_dir.glob('*.srt')).read_text())] == [str(i) for i in range(1, 101)]


def test_resume_after_partial_new_boundary_skips_completed_entries(tmp_path, pricing, price):
    args = args_for(tmp_path, 83)
    path, original = legacy_job(args, pricing, price, completed=75)
    args.resume, args.batch_size = True, 20
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    translated_ids = [row['id'] for call in client.responses.create.call_args_list
                      for row in json.loads(call.kwargs['input'])['translate']]
    assert translated_ids == [str(i) for i in range(76, 84)]
    assert json.loads(path.read_text())['requests'][:4] == original['requests']


def test_reasoning_change_preserves_spending_and_budget_blocks_api(tmp_path, pricing, price):
    args = args_for(tmp_path)
    path, original = legacy_job(args, pricing, price)
    args.resume, args.reasoning_effort, args.max_cost = True, 'low', Decimal('0.06')
    forbidden = Mock(side_effect=AssertionError('No client allowed'))
    with pytest.raises(tr.TranslationError, match='Budget guard'):
        tr.run(args, client_factory=forbidden, input_fn=lambda _: 'y')
    state = json.loads(path.read_text())
    assert state['requests'] == original['requests'] and tr.totals(state)[0] == Decimal('0.05030175')
    assert state['configuration_history'][-1]['config']['reasoning_effort'] == 'low'
    forbidden.assert_not_called()


def test_changed_batch_size_without_resume_cannot_reset_spending(tmp_path, pricing, price):
    args = args_for(tmp_path)
    path, _ = legacy_job(args, pricing, price)
    before = path.read_bytes()
    with pytest.raises(tr.TranslationError, match='--resume'):
        tr.run(args, client_factory=Mock(side_effect=AssertionError()))
    assert path.read_bytes() == before


def test_declined_resume_cannot_migrate_or_make_calls(tmp_path, pricing, price):
    args = args_for(tmp_path)
    path, _ = legacy_job(args, pricing, price)
    before = path.read_bytes()
    args.resume = True
    forbidden = Mock(side_effect=AssertionError('No client allowed'))
    tr.run(args, client_factory=forbidden, input_fn=lambda _: '')
    assert path.read_bytes() == before and not path.with_suffix('.json.bak').exists()
    forbidden.assert_not_called()


def test_confirmed_truncation_preserves_prior_spending_and_stops_without_retry(tmp_path, pricing, price, capsys):
    args = args_for(tmp_path)
    path, original = legacy_job(args, pricing, price)
    args.resume = True
    client = success_client()
    def incomplete(**request):
        return NS(status='incomplete', incomplete_details=NS(reason='max_output_tokens'), output=[],
                  output_text='{"entries":[', usage=NS(input_tokens=3460,
                  output_tokens=request['max_output_tokens'], input_tokens_details=NS(cached_tokens=0),
                  output_tokens_details=NS(reasoning_tokens=0)))
    client.responses.create.side_effect = incomplete
    with pytest.raises(tr.InvalidBatch, match='output_truncated'):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y', sleep=Mock(side_effect=AssertionError('No retry')))
    state = json.loads(path.read_text())
    latest = state['requests'][-1]
    assert state['requests'][:4] == original['requests'] and state['translations'] == {}
    assert latest['diagnostic']['category'] == 'output_truncated'
    assert latest['diagnostic']['expected_entries'] == 25
    assert latest['reasoning_tokens'] == 0 and latest['retry_decision'] == 'stop_nonrepairable'
    assert tr.totals(state)[0] == Decimal('0.05030175') + tr.cost(price, 3460, latest['max_output_tokens'])
    assert client.responses.create.call_count == 1 and not args.output_dir.exists()
    output = capsys.readouterr().out
    assert 'Incomplete reason: max_output_tokens' in output and 'Cost incurred:' in output


def test_ambiguous_ledgers_fail_closed(tmp_path, pricing, price):
    args = args_for(tmp_path)
    path, state = legacy_job(args, pricing, price)
    other = tr.new_state({**state['identity'], 'config': {**state['identity']['config'], 'batch_size': 25}})
    tr.save_state(path.parent / (tr.sha(tr.dumps(other['identity']).encode()) + '.json'), other)
    args.resume = True
    with pytest.raises(tr.TranslationError, match='Multiple compatible spending ledgers'):
        tr.run(args, client_factory=Mock(side_effect=AssertionError()))


@pytest.mark.parametrize('max_tokens', [0, -1, 128001])
def test_invalid_output_cap_no_client(tmp_path, max_tokens):
    args = args_for(tmp_path)
    args.max_output_tokens = max_tokens
    forbidden = Mock(side_effect=AssertionError())
    with pytest.raises(tr.TranslationError, match='must be positive'):
        tr.run(args, client_factory=forbidden)
    forbidden.assert_not_called()


def test_completed_gaps_keep_pending_dialogue_context_local(price):
    entries = tr.parse_srt(sample(8))
    cfg = tr.Config(batch_size=25, context=1)
    translated = {'3': 'Ahoj.', '4': 'Ahoj.', '7': 'Ahoj.'}
    plan = tr.planned_batches(entries, cfg, price, tr.TokenCounter(), translated)
    assert [[e.id for e in batch] for batch in plan] == [['1', '2'], ['5', '6'], ['8']]
    payload = tr.payload_for(entries, plan[1], cfg, translated, [])
    assert payload['before'][0]['id'] == '4' and payload['before'][0]['previous_translation'] == 'Ahoj.'
    assert payload['after'][0]['id'] == '7'


def test_multiple_configuration_changes_keep_first_backup_and_all_charges(tmp_path, pricing, price):
    args = args_for(tmp_path, 83)
    path, original = legacy_job(args, pricing, price, completed=75)
    before = path.read_bytes()
    args.resume = True
    tr.run(args, client_factory=success_client, input_fn=lambda _: 'y')
    after_first = json.loads(path.read_text())
    args.batch_size, args.reasoning_effort = 10, 'low'
    forbidden = Mock(side_effect=AssertionError('Completed checkpoint must not call API'))
    tr.run(args, client_factory=forbidden, input_fn=lambda _: 'y')
    after_second = json.loads(path.read_text())
    assert after_second['identity'] == original['identity']
    assert after_second['requests'] == after_first['requests']
    assert len(after_second['configuration_history']) == 2
    assert tr.totals(after_second) == tr.totals(after_first)
    assert path.with_suffix('.json.bak').read_bytes() == before
    forbidden.assert_not_called()


@pytest.mark.parametrize('setting,value', [('context', 4), ('target', 'de'), ('source', 'de')])
def test_translation_configuration_changes_cannot_reuse_legacy_checkpoint(tmp_path, pricing, price, setting, value):
    args = args_for(tmp_path)
    path, _ = legacy_job(args, pricing, price)
    before = path.read_bytes()
    args.resume = True
    setattr(args, setting, value)
    with pytest.raises(tr.TranslationError, match='No compatible checkpoint'):
        tr.run(args, client_factory=Mock(side_effect=AssertionError()))
    assert path.read_bytes() == before
