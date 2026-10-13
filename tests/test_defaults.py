"""Completed v1 defaults and all three CLI overrides reach actual SDK requests."""
import json

import translate_srt as tr
from test_truncation_fix import args_for, success_client


def test_cli_and_programmatic_defaults_match_production():
    args = tr.parser().parse_args(['input/example.srt'])
    config = tr.Config()
    assert (args.batch_size, args.reasoning_effort, args.response_schema) == (25, 'none', 'simple')
    assert (config.batch_size, config.reasoning_effort, config.response_schema) == (25, 'none', 'simple')


def test_default_run_uses_25_none_simple_without_flags(tmp_path):
    args = args_for(tmp_path, 30)
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    requests = [call.kwargs for call in client.responses.create.call_args_list]
    assert [len(json.loads(request['input'])['translate']) for request in requests] == [25, 5]
    for request in requests:
        assert request['reasoning'] == {'effort': 'none'}
        row = request['text']['format']['schema']['properties']['entries']['items']
        assert 'anyOf' not in row and row['properties']['text'] == {'type': 'string'}
    state = json.loads(next(args.checkpoint_dir.glob('*.json')).read_text())
    assert len(state['translations']) == 30
    assert all(row['response_schema_version'] == 4 for row in state['requests'])


def test_all_three_cli_overrides_reach_requests(tmp_path):
    defaults = args_for(tmp_path, 30)
    args = tr.parser().parse_args([str(defaults.input), '--batch-size', '10',
        '--reasoning-effort', 'low', '--response-schema', 'constrained',
        '--checkpoint-dir', str(defaults.checkpoint_dir), '--output-dir', str(defaults.output_dir)])
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    requests = [call.kwargs for call in client.responses.create.call_args_list]
    assert len(requests) == 3
    for request in requests:
        assert len(json.loads(request['input'])['translate']) == 10
        assert request['reasoning'] == {'effort': 'low'}
        assert 'anyOf' in request['text']['format']['schema']['properties']['entries']['items']
    state = json.loads(next(args.checkpoint_dir.glob('*.json')).read_text())
    assert all(row['response_schema_version'] == 3 for row in state['requests'])
    assert len(state['translations']) == 30
