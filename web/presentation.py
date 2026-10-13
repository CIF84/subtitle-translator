"""Read-only, evidence-based historical figures for the presentation layer.

Nothing here authorizes work, writes job data, or estimates future charges.
"""
from dataclasses import asdict
from pathlib import Path

import translate_srt as tr
from .service import read, safe_path


def _covered(records, entries, revision=False):
    """Accepted records must cover the complete source exactly once."""
    order = [e.id for e in entries]
    covered = set()
    for record in records:
        if revision:
            identifiers = record.get('batch_ids', [])
        else:
            validation = record.get('validation', {})
            count = validation.get('expected_entries')
            first = record.get('batch_first_id')
            if type(count) is not int or count < 1 or validation.get('received_entries') != count or first not in order:
                return False
            start = order.index(first)
            identifiers = order[start:start + count]
            if len(identifiers) != count:
                return False
        if not identifiers or len(identifiers) != len(set(identifiers)) or covered.intersection(identifiers):
            return False
        covered.update(identifiers)
    return covered == set(order)


def _translation(service, job, entries, raw):
    config = tr.Config(source=job['config']['source'], target=job['config']['target'],
                       model=job['config']['translation_model'])
    pricing, price = tr.load_pricing(service.root / 'pricing.json', config.model)
    identity, _ = tr.identity_for(raw, config, pricing)
    relevant = set(asdict(config)) - {'retries', 'max_output_tokens'}
    candidates = list((service.root / 'checkpoints').glob('*.json'))
    candidates += list((service.root / 'checkpoints/web').glob('*/translation.json'))
    observations = []
    for path in candidates:
        if path == service.checkpoint(job['id']):
            continue
        try:
            path = safe_path(service.root, path.relative_to(service.root))
            saved = read(path)
            original = saved['identity']
            if any(original.get(k) != identity[k] for k in ('source_sha256', 'pricing_sha256')):
                continue
            state = tr.load_state(path, identity, entries, config, price)
            if set(state['translations']) != {e.id for e in entries}:
                continue
            # Resumed v1 ledgers deliberately retain their original identity.
            # Each accepted request must instead match a verified configuration
            # history entry; failed legacy attempts never qualify as successes.
            configurations = [original, *state.get('configuration_history', [])]
            allowed = set()
            for item in configurations:
                if item.get('prompt_sha256') != identity['prompt_sha256']:
                    continue
                previous = tr.Config(**item['config'])
                if all(asdict(previous)[k] == asdict(config)[k] for k in relevant):
                    allowed.add(tr.sha(tr.dumps(asdict(previous)).encode()))
            records = [r for r in state['requests'] if r['status'] == 'settled'
                       and r.get('validation', {}).get('result') == 'passed']
            if not records or any(r.get('configuration_sha256') not in allowed
                                  or r.get('response_schema_mode') != config.response_schema
                                  or r.get('reasoning_effort') != config.reasoning_effort
                                  or r.get('response_status') != 'completed' or r.get('refusal') for r in records):
                continue
            if not _covered(records, entries):
                continue
            amount = tr.totals({'requests': records})[0]
            observations.append(dict(cost=str(amount), entries=len(entries), model=config.model,
                operation='translation', completed_at=path.stat().st_mtime,
                includes_successful_probes=any(r.get('mode') == 'probe' for r in records)))
        except (tr.TranslationError, OSError, KeyError, TypeError, ValueError):
            continue  # Ineligible/corrupt evidence is omitted, never guessed or repaired.
    return observations


def _revision(service, job, entries):
    expected = service.revision_plan(job)['identity']
    observations = []
    for folder in (service.root / 'jobs').iterdir():
        if folder.name == job['id'] or not folder.is_dir():
            continue
        try:
            previous = service.job(folder.name)
            if not previous['revision_complete'] or previous['source_sha256'] != job['source_sha256']:
                continue
            state = service.revision_state(previous)
            if state['identity'] != expected or set(state['completed_ids']) != {e.id for e in entries}:
                continue
            records = [r for r in state['requests'] if r['status'] == 'settled' and r.get('validation') == 'passed']
            if any(r.get('response_status') != 'completed' or r.get('refusal') for r in records) or not _covered(records, entries, revision=True):
                continue
            amount = tr.totals({'requests': records})[0]
            observations.append(dict(cost=str(amount), entries=len(entries), model=expected['model'],
                operation='revision', completed_at=(folder / 'revision-ledger.json').stat().st_mtime,
                includes_successful_probes=False))
        except (tr.TranslationError, OSError, KeyError, TypeError, ValueError):
            continue
    return observations


def history(service, ident, operation):
    job = service.job(ident)
    raw, entries = service.source(job)
    observations = _translation(service, job, entries, raw) if operation == 'translation' else _revision(service, job, entries)
    return {'comparable': max(observations, key=lambda r: r['completed_at']) if observations else None}
