"""Authored synthetic fixtures cover wrapping, cue boundaries, styling and speakers."""
from decimal import Decimal
import json
import re
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import translate_srt as tr
from test_truncation_fix import args_for, legacy_job, success_client


def cue(text):
    return tr.Entry('1', '00:02:13,250 --> 00:02:16,500', text)


@pytest.mark.parametrize('original,translated', [
    ('We should leave\nright now.', 'Měli bychom hned odejít.'),
    ('We should leave.', 'Měli bychom\nodejít.'),
    ('Hello, my friend!', 'Příteli, ahoj!'),
    ('<i>Hello, my friend.</i>', '<i>Ahoj,\npříteli.</i>'),
    ('<i>Hello</i>\n<i>friend.</i>', '<i>Ahoj, příteli.</i>'),
    ('<i>Hello, my friend.</i>', '<i>Ahoj,</i>\n<i>příteli.</i>'),
    ('<i>Hello</i>, friend.', 'Příteli, <i>ahoj</i>!'),
    ('<i><b>Hello</b></i>\n<i><b>friend.</b></i>', '<i><b>Ahoj,\npříteli.</b></i>'),
    ('[Nora]\n<i>The terrarium needs a lamp.</i>', '[Nora] <i>Terárium potřebuje lampu.</i>'),
    ('[Nora]\n<i>The terrarium needs a lamp.</i>', '[Nora]\n<i>Terárium potřebuje\nlampu.</i>'),
    ('JOHN: Hello,\nmy friend.', 'JOHN: Ahoj, příteli.'),
    ('MAN: Hello.', 'MUŽ: Ahoj.'),
    ('MAN 2: Hello.', 'MUŽ 2: Ahoj.'),
    ('[MAN]\nHello.', '[MUŽ] Ahoj.'),
    ('- Hello, my friend.\n- How are you?', '– Ahoj,\npříteli.\n— Jak se máš?'),
    ('JOHN: Hello.\nMARY: Goodbye.', 'JOHN: Ahoj,\npříteli.\nMARY: Čau.'),
    ('[John]\nHello.\n[Mary]\nGoodbye.', '[John] Ahoj.\n[Mary] Čau.'),
    ('[door slams]\n[gentle music]', '[bouchnutí dveří] [jemná hudba]'),
    ('[door slams]\nAre you there?', '[bouchnutí dveří] Jsi tam?'),
    ('<i>[door slams]</i>\nAre you there?', '<i>[bouchnutí dveří]</i> Jsi\ntam?'),
    ('♪ [gentle\nmusic] ♪', '♪ [jemná hudba] ♪'),
    (r'{\an8}<font color="#fff">Hello.</font>', '{\\an8}<font color="#fff">Ahoj,\npříteli.</font>'),
    ('Value < 3 and {x}.', 'Hodnota < 3\na {x}.'),
    ('<i>…</i>', '<i>...</i>'),
])
def test_legitimate_wrapping_and_linguistic_changes(original, translated):
    entry = cue(original)
    assert re.fullmatch(tr.formatting_pattern(entry), translated)
    tr.validate_text(entry, translated)
    translated_entries = tr.parse_srt(tr.render_output([entry], {'1': translated}))
    assert translated_entries[0].id == entry.id and translated_entries[0].timing == entry.timing
    assert translated_entries[0].text == translated


@pytest.mark.parametrize('original,translated,rule', [
    ('Hello.\nHow are you?', '\nAhoj.', 'blank_line'),
    ('[Nora]\n<i>The terrarium needs a lamp.</i>', '[Nora]\n ', 'blank_line'),
    ('[Nora]\n<i>The terrarium needs a lamp.</i>', '\n<i>Terárium potřebuje lampu.</i>', 'blank_line'),
    ('Hello.', 'Ahoj.\n\nKam jdeš?', 'blank_line'),
    ('- Hello.\n- Goodbye.', '- Ahoj. - Čau.', 'speaker_turn_markers_changed'),
    ('- Hello.\n- Goodbye.', 'Ahoj.\nČau.', 'speaker_turn_markers_changed'),
    ('JOHN: Hello.\nMARY: Goodbye.', 'JOHN: Ahoj. MARY: Čau.', 'speaker_turn_markers_changed'),
    ('JOHN: Hello.\nMARY: Goodbye.', 'MARY: Ahoj.\nJOHN: Čau.', 'speaker_identity_changed'),
    ('[John]\nHello.\n[Mary]\nGoodbye.', '[John] Ahoj. [Mary] Čau.', 'speaker_label_not_at_turn_start'),
    ('[Nora]\n<i>The terrarium needs a lamp.</i>', '[Other] <i>Terárium potřebuje lampu.</i>', 'speaker_identity_changed'),
    ('MAN 2: Hello.', 'MUŽ: Ahoj.', 'speaker_number_changed'),
    ('<i>Hello.</i>', '<i>Ahoj.', 'unclosed_tag'),
    ('<i>Hello.</i>', '<i>Ahoj.</b>', 'mismatched_closing_tag'),
    ('<i>Hello.</i>', 'Ahoj.', 'formatting_style_changed'),
    ('Hello.', '<i>Ahoj.</i>', 'formatting_style_changed'),
    ('<i>Hello.</i>', '<b>Ahoj.</b>', 'formatting_style_changed'),
    ('<i>Hello</i> friend.', '<i>Ahoj příteli.</i>', 'formatting_style_changed'),
    ('<i>JOHN:</i> Hello.', 'JOHN: <i>Ahoj.</i>', 'formatting_style_changed'),
    ('<i>- Hello.</i>\n- Goodbye.', '- Ahoj.\n<i>- Čau.</i>', 'formatting_style_changed'),
    ('<i>♪</i>', '<i></i>♪', 'formatting_style_changed'),
    ('<i>Hello.</i>', '<i Ahoj.</i>', 'malformed_tag'),
    ('<i>Hello.</i>', '<i>Ahoj.</i', 'unclosed_tag'),
    ('<font color="red">Hello.</font>', '<font color="blue">Ahoj.</font>', 'formatting_style_changed'),
    ('<font color="red">Hello.</font>', '<font color="red>Ahoj.</font>', 'malformed_tag'),
    ('<i><b>Hello.</b></i>', '<i><b>Ahoj.</i></b>', 'mismatched_closing_tag'),
    ('[door slams]', 'bouchnutí dveří', 'sdh_music_markers_changed'),
    ('[door slams]', '[ ]', 'empty_sdh_description'),
    ('[door slams]', '[bouchnutí [dveří]]', 'sdh_music_markers_changed'),
    ('♪ [music] ♪', '[hudba]', 'sdh_music_markers_changed'),
    (r'{\an8}Hello.', r'{\an7}Ahoj.', 'formatting_style_changed'),
])
def test_structural_losses_and_malformed_output_are_rejected(original, translated, rule):
    with pytest.raises(tr.InvalidBatch) as error:
        tr.validate_text(cue(original), translated)
    details = error.value.details
    assert details['rule'] == rule and details['entry_id'] == '1'
    assert details['expected_lines'] == len(original.split('\n'))
    assert details['received_lines'] == len(translated.split('\n'))
    assert isinstance(details['expected_markers'], list) and isinstance(details['received_markers'], list)


def test_same_line_counts_blank_line_rule_is_precise_and_content_free():
    source, text = cue('[Nora]\n<i>The terrarium needs a lamp.</i>'), '\n<i>Terárium potřebuje lampu.</i>'
    with pytest.raises(tr.InvalidBatch) as caught:
        tr.validate_text(source, text)
    error = caught.value
    assert error.category == 'line_structure'
    assert error.details['rule'] == 'blank_line'
    assert error.details['expected_lines'] == error.details['received_lines'] == 2
    assert error.details['blank_line_numbers'] == [1] and error.details['blank_line_count'] == 1
    assert '<i>' in error.details['received_markers'] and '[' in error.details['expected_markers']
    assert 'Nora' not in tr.dumps(error.details) and 'Terárium' not in tr.dumps(error.details)


@pytest.mark.parametrize('text', ['\nAhoj.', 'Ahoj.\n', 'Ahoj.\n\nČau.', 'Ahoj.\n \t\nČau.'])
def test_schema_forbids_blank_lines_instead_of_fixed_wrapping(text):
    assert not re.fullmatch(tr.formatting_pattern(cue('Hello.')), text)


def test_batch_and_final_validation_allow_rewrapping_but_keep_order_and_timestamps():
    entries = [cue('We should leave\nright now.'), tr.Entry('2', '00:02:17,125 --> 00:02:19,750', '<i>Hello.</i>')]
    raw = tr.dumps({'entries': [{'id': '2', 'text': '<i>Ahoj,\npříteli.</i>'},
                               {'id': '1', 'text': 'Měli bychom hned odejít.'}], 'glossary': []})
    translations, _ = tr.validate_batch(raw, entries, [])
    rendered = tr.parse_srt(tr.render_output(entries, translations))
    assert [(e.id, e.timing) for e in rendered] == [(e.id, e.timing) for e in entries]
    assert [e.text for e in rendered] == ['Měli bychom hned odejít.', '<i>Ahoj,\npříteli.</i>']


def test_new_diagnostics_are_logged_without_subtitle_content(tmp_path, capsys):
    source = tmp_path/'test.srt'
    source.write_text('1\n00:02:13,250 --> 00:02:16,500\n[Nora]\n<i>The terrarium needs a lamp.</i>\n\n')
    args = tr.parser().parse_args([str(source), '--probe', '1', '--checkpoint-dir', str(tmp_path/'cp'), '--output-dir', str(tmp_path/'out')])
    client = Mock()
    client.responses.create.return_value = NS(status='completed', output=[], output_text=tr.dumps({
        'entries': [{'id': '1', 'text': '[Nora]\n \n<i>Terárium potřebuje lampu.</i>'}], 'glossary': []}),
        usage=NS(input_tokens=601, output_tokens=96, input_tokens_details=NS(cached_tokens=0),
                 output_tokens_details=NS(reasoning_tokens=0)))
    with pytest.raises(tr.InvalidBatch):
        tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    output = capsys.readouterr().out
    for value in ('Entry: 1', 'Lines expected/received: 2 / 3', 'Rule violated: blank_line',
                  'Expected formatting markers:', 'Received formatting markers:', 'Blank line numbers: 2'):
        assert value in output
    assert 'Nora' not in output and 'Terárium' not in output and 'The terrarium' not in output
    state = json.loads(next(args.checkpoint_dir.glob('*.json')).read_text())
    assert state['translations'] == {} and tr.totals(state)[0] == Decimal('0.00088275')
    assert state['requests'][0]['diagnostic']['rule'] == 'blank_line'


def test_original_prompt_checkpoint_resume_preserves_cost_and_work(tmp_path):
    pricing, price = tr.load_pricing(tr.BASE/'pricing.json', 'gpt-5.4-mini')
    args = args_for(tmp_path, 78)
    path, state = legacy_job(args, pricing, price, completed=75)
    state['identity']['prompt_version'] = '1.0'
    state['identity']['prompt_sha256'] = tr.LEGACY_PROMPT_SHA256
    state['translations']['1'] = '  Ahoj, příteli. \t'
    state['requests'].append({'status': 'settled', 'reservation': '0.02171625', 'input_tokens': 601,
                              'output_tokens': 96, 'cached_tokens': 0, 'cost': '0.00088275'})
    tr.save_state(path, state)
    before = path.read_bytes()
    args.resume, args.probe, args.dry_run = True, 3, True
    forbidden = Mock(side_effect=AssertionError('Dry run cannot call API'))
    tr.run(args, client_factory=forbidden)
    assert path.read_bytes() == before
    args.dry_run = False
    client = success_client()
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    loaded = json.loads(path.read_text())
    assert loaded['identity'] == state['identity']
    assert loaded['requests'][:5] == state['requests']
    assert {k: loaded['translations'][k] for k in state['translations']} == state['translations']
    assert loaded['configuration_history'][-1]['prompt_version'] == '2.0'
    assert loaded['configuration_history'][-1]['config']['formatting_policy'] == '2.0'
    assert tr.totals(loaded)[0] == Decimal('0.05178450')
    assert loaded['requests'][-1]['response_schema_version'] == 4
    assert len(list(args.checkpoint_dir.glob('*.json'))) == 1
    selected = json.loads(client.responses.create.call_args.kwargs['input'])['translate']
    assert [row['id'] for row in selected] == ['76', '77', '78']


def test_unrecognized_prompt_change_is_incompatible_even_with_policy_field():
    pricing, _ = tr.load_pricing(tr.BASE/'pricing.json', 'gpt-5.4-mini')
    current, _ = tr.identity_for(b'content', tr.Config(), pricing)
    legacy = {**current, 'prompt_version': '1.0', 'prompt_sha256': tr.LEGACY_PROMPT_SHA256}
    unknown = {**current, 'prompt_sha256': '0'*64}
    assert tr.job_family(current) == tr.job_family(legacy)
    assert tr.job_family(current) != tr.job_family(unknown)


def test_diagnostic_marker_lists_are_bounded():
    source = cue('[noise] ' * 100)
    with pytest.raises(tr.InvalidBatch) as caught:
        tr.validate_text(source, '\n' + '[hluk] '*100)
    assert len(caught.value.details['expected_markers']) == tr.DIAGNOSTIC_MARKER_LIMIT + 1
    assert 'more markers' in caught.value.details['expected_markers'][-1]


@pytest.mark.parametrize('original,translated', [
    ('[DOOR SLAMS]', '[bouchnutí dveří]'),
    ('[PHONE RINGING]\nHello.', '[zvonění telefonu] Ahoj.'),
    ('[GUNSHOT]', '[výstřel]'),
    ('JOHN (whispering): Hello.', 'JOHN (šeptem): Ahoj.'),
    ('[Nora (whispering)]\nHello.', '[Nora (šeptem)] Ahoj.'),
    ('OLD MAN: Hello.', 'STARÝ MUŽ: Ahoj.'),
    ('FRAU: Hello.', 'ŽENA: Ahoj.'),
])
def test_roles_and_sdh_annotations_translate_without_false_name_rules(original, translated):
    tr.validate_text(cue(original), translated)


@pytest.mark.parametrize('original,translated,rule', [
    ('JOHN (whispering): Hello.', 'JOHN: Ahoj.', 'speaker_annotation_changed'),
    ('[Nora (whispering)]\nHello.', '[Nora] Ahoj.', 'speaker_annotation_changed'),
    ('JOHN (whispering): Hello.', 'OTHER (šeptem): Ahoj.', 'speaker_identity_changed'),
    ('Hello.', r'{\an8Ahoj.', 'malformed_ass_override'),
])
def test_missing_speaker_annotations_and_new_malformed_ass_rejected(original, translated, rule):
    with pytest.raises(tr.InvalidBatch) as error:
        tr.validate_text(cue(original), translated)
    assert error.value.details['rule'] == rule


def test_empty_formatted_line_remains_invalid_with_equal_marker_types():
    with pytest.raises(tr.InvalidBatch) as error:
        tr.validate_text(cue('<i>Hello.</i>'), '<i></i>\n<i>Ahoj.</i>')
    assert error.value.details['rule'] == 'empty_visible_line'
    assert error.value.details['blank_line_numbers'] == [1]


def test_synthetic_probe_shape_accepts_rewrapped_speaker_label_and_preserves_all_costs(tmp_path):
    pricing, price = tr.load_pricing(tr.BASE/'pricing.json', 'gpt-5.4-mini')
    args = args_for(tmp_path, 12)
    args.input.write_text(args.input.read_text().replace('Hello, my friend.', '[Nora]\n<i>The terrarium needs a lamp.</i>', 1))
    path, state = legacy_job(args, pricing, price)
    state['identity']['prompt_version'], state['identity']['prompt_sha256'] = '1.0', tr.LEGACY_PROMPT_SHA256
    for inp, out in [(1457, 3072), (601, 96)]:
        state['requests'].append({'status': 'settled', 'reservation': '0.02171625', 'input_tokens': inp,
                                  'output_tokens': out, 'cached_tokens': 0, 'cost': str(tr.cost(price, inp, out))})
    tr.save_state(path, state)
    assert tr.totals(state)[0] == Decimal('0.06610125')
    args.resume, args.probe = True, 5
    client = Mock()
    client.responses.create.return_value = NS(status='completed', output=[], output_text=tr.dumps({
        'entries': [{'id': str(i), 'text': '\n[Nora] <i>Terárium potřebuje lampu.</i>\n ' if i==1 else 'Ahoj, příteli.'} for i in range(1,6)],
        'glossary': []}), usage=NS(input_tokens=601, output_tokens=96, input_tokens_details=NS(cached_tokens=0),
                                 output_tokens_details=NS(reasoning_tokens=0)))
    tr.run(args, client_factory=lambda: client, input_fn=lambda _: 'y')
    client.responses.create.assert_called_once()
    client.models.retrieve.assert_not_called()
    loaded = json.loads(path.read_text())
    assert loaded['requests'][:6] == state['requests'] and loaded['identity'] == state['identity']
    assert loaded['translations']['1'] == '[Nora] <i>Terárium potřebuje lampu.</i>'
    assert loaded['requests'][-1]['validation']['result'] == 'passed'
    assert tr.totals(loaded)[0] == Decimal('0.06698400')
    assert not args.output_dir.exists()


@pytest.mark.parametrize('text', ['\n[Nora] <i>Terárium potřebuje lampu.</i>', '[Nora] <i>Terárium potřebuje lampu.</i>\n ',
                                 '\n \n[Nora] <i>Terárium potřebuje lampu.</i>\n\t'])
def test_outer_blank_padding_is_normalized_only_for_new_responses(text):
    entry = cue('[Nora]\n<i>The terrarium needs a lamp.</i>')
    data = tr.dumps({'entries': [{'id': '1', 'text': text}], 'glossary': []})
    translations, _ = tr.validate_batch(data, [entry], [])
    assert translations == {'1': '[Nora] <i>Terárium potřebuje lampu.</i>'}
    assert tr.parse_srt(tr.render_output([entry], translations))[0].text == translations['1']


@pytest.mark.parametrize('text', ['\n<i>Terárium potřebuje lampu.</i>', '[Nora]\n ', '\n[Other] <i>Terárium potřebuje lampu.</i>\n'])
def test_padding_normalization_cannot_hide_lost_dialogue_format_or_speaker(text):
    entry = cue('[Nora]\n<i>The terrarium needs a lamp.</i>')
    data = tr.dumps({'entries': [{'id': '1', 'text': text}], 'glossary': []})
    with pytest.raises(tr.InvalidBatch) as error:
        tr.validate_batch(data, [entry], [])
    assert error.value.details['raw_received_lines'] == len(text.split('\n'))
    assert error.value.details['normalized_received_lines'] == 1


def test_padding_normalization_never_strips_forbidden_characters_or_internal_blanks():
    entry = cue('Hello.')
    for text in ['Ahoj.\r', 'Ahoj.\x00', '\ufeffAhoj.', 'Ahoj.\n \nČau.']:
        with pytest.raises(tr.InvalidBatch):
            tr.validate_batch(tr.dumps({'entries': [{'id': '1', 'text': text}], 'glossary': []}), [entry], [])
