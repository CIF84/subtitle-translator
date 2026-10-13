"""Authored WP2 contract fixtures: no private benchmark or episode text required.

The 816/52 shape and selected identifiers exercise the original runner contract.
Reference labels below are synthetic test branches, not linguistic judgments.
"""
from pathlib import Path
import shutil

import revision_experiment as rev
import translate_srt as tr

SELECTED = '1 3 7 14 17 18 19 31 38 49 55 58 71 102 121 143 167 192 209 212 231 233 267 281 298 328 329 337 402 414 418 438 462 485 486 511 539 556 570 595 613 633 666 675 681 682 714 741 743 757 759 803'.split()
HIGH = set('1 38 143 192 328 418 438 486 556 595 682 743 759'.split())
CONTROLS = set('3 7 14 49 121 231 233 570 803'.split())
UNRESOLVED = set('102 167 281 613 714'.split())


def build_project(root: Path, source_root: Path):
    """Write independent synthetic sources and correctly hashed references in tmp_path."""
    for name in ('translate_srt.py', 'pricing.json'):
        shutil.copyfile(source_root / name, root / name)
    en, cs = [], []
    for n in range(1, 817):
        timing = f'00:{n // 60:02}:{n % 60:02},000 --> 00:{(n + 1) // 60:02}:{(n + 1) % 60:02},000'
        english = f'Test cue {n}: please bring the green folder to the library tomorrow morning so everyone can read the notes before our appointment.'
        czech = f'Zkušební replika {n}: přines zelenou složku.'
        if n == 1:
            english, czech = '[Alex]\n<i>Arrange a call.</i>', '[Alex]\n<i>On chce telefonovat.</i>'
        elif n == 7:
            english, czech = '[Blair over phone]\n<i>I will handle the folder.</i>', '[Blair přes telefon]\n<i>Postarám se o složku.</i>'
        elif n == 143:
            english, czech = '- Can you fetch the folder?\n- [engine starts]', '- Přineseš složku?\n- [motor startuje]'
        elif n == 595:
            english, czech = '[person laughing]', '[člověk se směje]'
        en.append(tr.Entry(str(n), timing, english))
        cs.append(tr.Entry(str(n), timing, czech))
    for name, entries in (('input/synthetic.en.srt', en), ('output/synthetic.cs.srt', cs)):
        tr.atomic_write(root / name, '\n\n'.join(f'{e.id}\n{e.timing}\n{e.text}' for e in entries) + '\n')
    cases = []
    for ident in SELECTED:
        i = int(ident) - 1
        references = ['557'] if ident == '556' else ['744'] if ident == '743' else []
        cases.append(dict(id=ident, source_position=i + 1, timestamp=en[i].timing,
                          english=en[i].text, baseline_czech=cs[i].text,
                          context=dict(before=[dict(id=e.id, english=e.text) for e in en[max(0, i-5):i]],
                                       after=[dict(id=e.id, english=e.text) for e in en[i+1:i+6]],
                                       review_references=[dict(id=n, english=en[int(n)-1].text) for n in references])))
    b = dict(benchmark_id='synthetic-contract-test', selection=dict(selected_ids=SELECTED), cases=cases,
             provenance=dict(english_file='input/synthetic.en.srt', czech_file='output/synthetic.cs.srt',
                             english_sha256=rev.digest(root / 'input/synthetic.en.srt'),
                             czech_sha256=rev.digest(root / 'output/synthetic.cs.srt'),
                             production_engine_sha256=rev.digest(root / 'translate_srt.py'),
                             v1_checkpoint_sha256=tr.sha(b'synthetic checkpoint identity'), v1_cumulative_cost_usd='0'))
    path = root / 'benchmarks/mobland-v1-baseline.json'
    rev.save_json(path, b)
    refs = []
    for c in cases:
        ident = c['id']
        status = ('user_confirmed_error' if ident == '1' else 'high_confidence_textual_error' if ident in HIGH
                  else 'acceptable_control' if ident in CONTROLS else 'unresolved_source' if ident in UNRESOLVED else 'suspected_issue')
        refs.append(dict(**{k: c[k] for k in ('id', 'timestamp', 'english', 'baseline_czech')},
                         reference_status=status, issue_group=f'synthetic-{ident}',
                         applicability=dict.fromkeys(rev.WEIGHTS, True),
                         acceptance_criterion='Synthetic validation branch; no real quality judgment.',
                         numeric_ground_truth=None))
    r = dict(version=1, benchmark_sha256=rev.digest(path), weights=rev.WEIGHTS, cases=refs,
             high_confidence_ids=[c['id'] for c in refs if c['id'] in HIGH],
             control_ids=[c['id'] for c in refs if c['id'] in CONTROLS],
             unresolved_ids=[c['id'] for c in refs if c['id'] in UNRESOLVED])
    rev.save_json(root / 'benchmarks/mobland-v1-revision-references.json', r)
