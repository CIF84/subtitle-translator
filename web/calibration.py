"""Optional consumption forecast. No spending authority or engine changes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import argparse
import json

import translate_srt as tr
import revision_experiment as rev
import revision_calibration as samples
from .service import safe_path, WebError

VERSION = 1
METHOD = 'pooled-actual-to-proxy-token-ratios-v1'
MIN_SAMPLES = 3
MIN_CUES = 75
MAX_AGE_DAYS = 90
FIELDS = ('model','source','target','prompt_version','prompt_sha256','schema_version',
          'batch_size','context','reasoning')


def identity(production, tokenizer):
    return {**{k:production[k] for k in FIELDS},'tokenizer':tokenizer}


def _hash(path):
    return tr.sha(path.read_bytes())


def import_samples(root):
    """Explicit offline import from verified request records; never read summary.json.

    Output is an immutable evidence index, not a second spending ledger. Each
    observation preserves its historical rates/cost; forecast repricing is separate.
    """
    root=Path(root).resolve()
    plan=samples.build(root)
    manifest,state=samples.load(root,plan)
    origin=samples.folder(root)
    files={}
    def evidence(path):
        path=safe_path(root,path.relative_to(root))
        files[str(path.relative_to(root))]=_hash(path)
        return rev.read_json(path)
    evidence(origin/'selection.json');evidence(origin/'cost-ledger.json')
    observations=[]
    for record in state['requests']:
        if record['status']!='settled' or record.get('validation')!='passed':continue
        name=record['sample'];spec=manifest['protocol']['samples'][name]
        result=evidence(origin/'results'/f"{record['id']}.json")
        capture=evidence(origin/'responses'/f"{record['id']}.json")
        if result['status']!='completed' or result['request_id']!=record['id'] or result['sample']!=name or result['request_sha256']!=spec['request_sha256'] or result['estimate']!=spec['estimate']:
            raise WebError('Calibration result identity does not match authoritative records.')
        metadata=capture['response_metadata']
        if record.get('response_status')!='completed' or record.get('refusal') or metadata['response_status']!='completed' or metadata.get('refusal') or metadata.get('incomplete_reason'):
            raise WebError('Calibration response is not complete.')
        if capture['attempt_id']!=record['id'] or capture['protocol_hash']!=state['selection_sha256'] or capture['prompt_version']!=plan['protocol']['production_identity']['prompt_version'] or capture['request_schema_version']!=plan['protocol']['production_identity']['schema_version']:
            raise WebError('Calibration capture identity differs.')
        if capture['production_prompt_sha256']!=plan['protocol']['production_identity']['prompt_sha256'] or capture['output_limit']!=spec['estimate']['output_cap']:
            raise WebError('Calibration response configuration differs.')
        for k in ('input_tokens','output_tokens','cached_tokens'):
            if result['usage'][k]!=record[k] or capture['usage'][k]!=record[k]:raise WebError('Calibration usage does not match authoritative records.')
        if Decimal(result['usage']['cost'])!=Decimal(record['cost']):raise WebError('Calibration cost does not match authoritative accounting.')
        audit=rev.audit_corrections(capture['raw_generated_text'],plan['samples'][name]['cases'])
        if audit['validation_errors'] or any(audit[k]!=result[k] for k in ('proposal_count','corrections','unchanged_proposals','rejected_proposals','validation_errors')):
            raise WebError('Calibration response fails production validation.')
        approved=datetime.fromisoformat(record['approved_at'])
        if approved.tzinfo is None:raise WebError('Calibration measurement date lacks a timezone.')
        measured=approved+timedelta(seconds=record['elapsed_seconds'])
        observations.append(dict(request_id=record['id'],source_sha256=manifest['protocol']['production_identity']['source_sha256'],
            cues=len(spec['ids']),input_tokens=record['input_tokens'],output_tokens=record['output_tokens'],
            cached_tokens=record['cached_tokens'],actual_cost=record['cost'],historical_rates=record['rates'],
            estimated_input_tokens=spec['estimate']['input_tokens'],estimated_output_tokens=spec['estimate']['output_tokens'],
            measured_at=measured.isoformat(),proposed_corrections=audit['proposal_count']))
    if not observations:raise WebError('No authoritative completed calibration observations available.')
    observations.sort(key=lambda r:r['request_id'])
    document=dict(version=VERSION,method=METHOD,
        identity=identity(plan['protocol']['production_identity'],plan['protocol']['tokenizer']),
        observations=observations,evidence_files=files,
        selection_limitations='Small nonrandom contiguous samples; early sample includes a known ambiguity. Proposal counts do not prove correction accuracy.')
    dest=safe_path(root,'calibrations','revision');dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest.chmod(0o700)
    target=dest/(tr.sha(tr.dumps(document).encode())+'.json')
    if target.exists():
        if rev.read_json(target)!=document:raise WebError('Existing calibration index differs; preserved.')
    else:
        tr.atomic_write(target,json.dumps(document,ensure_ascii=True,indent=2)+'\n');target.chmod(0o600)
    return target


def _eligible(root, path, match, now):
    """Fail closed for corrupt, stale, altered or unsupported calibration evidence."""
    path=safe_path(root,path.relative_to(root));record=rev.read_json(path)
    if path.stem!=tr.sha(tr.dumps(record).encode()):raise ValueError
    if record['version']!=VERSION or record['method']!=METHOD or record['identity']!=match:raise ValueError
    proofs=record['evidence_files']
    if not proofs:raise ValueError
    for name,digest in proofs.items():
        parts=Path(name)
        if parts.is_absolute() or '..' in parts.parts or parts.parts[:2]!=('experiments','revision-calibration'):raise ValueError
        if _hash(safe_path(root,parts))!=digest:raise ValueError
    # Bind derived observations/configuration to the original authoritative records,
    # even if someone edits the index and renames its content-addressed filename.
    origin=safe_path(root,'experiments','revision-calibration')
    manifest=rev.read_json(origin/'selection.json')
    ledger=rev.read_json(origin/'cost-ledger.json')
    if record['identity']!=identity(manifest['protocol']['production_identity'],manifest['protocol']['tokenizer']):raise ValueError
    if ledger['selection_sha256']!=tr.sha(tr.dumps(manifest).encode()):raise ValueError
    originals={r['id']:r for r in ledger['requests']}
    observations=record['observations'];seen=set()
    for o in observations:
        if o['request_id'] in seen:raise ValueError
        seen.add(o['request_id'])
        measured=datetime.fromisoformat(o['measured_at'])
        if measured.tzinfo is None or measured>now or now-measured>timedelta(days=MAX_AGE_DAYS):raise ValueError
        if any(type(o[k]) is not int or o[k]<0 for k in ('input_tokens','output_tokens','cached_tokens','cues','estimated_input_tokens','estimated_output_tokens')):raise ValueError
        if not o['cues'] or not o['estimated_input_tokens'] or not o['estimated_output_tokens'] or o['cached_tokens']>o['input_tokens']:raise ValueError
        if Decimal(o['actual_cost'])!=tr.cost(o['historical_rates'],o['input_tokens'],o['output_tokens'],o['cached_tokens']):raise ValueError
        r=originals[o['request_id']]
        spec=manifest['protocol']['samples'][r['sample']]
        if r['status']!='settled' or r.get('validation')!='passed' or r.get('response_status')!='completed' or r.get('refusal'):raise ValueError
        if any(o[k]!=r[k] for k in ('input_tokens','output_tokens','cached_tokens')) or o['actual_cost']!=r['cost'] or o['historical_rates']!=r['rates']:raise ValueError
        if o['cues']!=len(spec['ids']) or o['estimated_input_tokens']!=spec['estimate']['input_tokens'] or o['estimated_output_tokens']!=spec['estimate']['output_tokens']:raise ValueError
        expected_date=datetime.fromisoformat(r['approved_at'])+timedelta(seconds=r['elapsed_seconds'])
        if measured!=expected_date or o['source_sha256']!=manifest['protocol']['production_identity']['source_sha256']:raise ValueError
    if len(observations)<MIN_SAMPLES or sum(o['cues'] for o in observations)<MIN_CUES:raise ValueError
    return record


def forecast(root, plan, *, now=None):
    """Only recalibrate normal token predictions. Request caps/guards remain external."""
    root=Path(root).resolve();now=now or datetime.now(timezone.utc)
    match=identity(plan['identity'],tr.TokenCounter().label)
    options=[]
    directory=safe_path(root,'calibrations','revision')
    for path in sorted(directory.glob('*.json')):
        try:options.append(_eligible(root,path,match,now))
        except (OSError,tr.TranslationError,ValueError,KeyError,TypeError,ArithmeticError):continue
    if not options:
        return dict(kind='heuristic',reason='No current matching calibration with at least 3 samples / 75 cues.',evidence=None)
    # Choose one newest verified collection. Never pool overlapping reimports twice.
    chosen=max(options,key=lambda r:max(o['measured_at'] for o in r['observations']))
    obs=chosen['observations']
    i=sum(o['input_tokens'] for o in obs)/sum(o['estimated_input_tokens'] for o in obs)
    o=sum(o['output_tokens'] for o in obs)/sum(o['estimated_output_tokens'] for o in obs)
    inp=sum(p['estimate'].input_tokens for p in plan['plans'])
    out=sum(p['estimate'].output_tokens for p in plan['plans'])
    projected_i=round(inp*i);projected_o=round(out*o)
    def amount(ir,orr):return str(tr.cost(plan['price'],round(inp*ir),round(out*orr)))
    irs=[x['input_tokens']/x['estimated_input_tokens'] for x in obs]
    ors=[x['output_tokens']/x['estimated_output_tokens'] for x in obs]
    dates=sorted(x['measured_at'][:10] for x in obs)
    return dict(kind='calibrated',expected_cost=str(tr.cost(plan['price'],projected_i,projected_o)),
        input_tokens=projected_i,output_tokens=projected_o,
        sensitivity_low=amount(min(irs),min(ors)),sensitivity_high=amount(max(irs),max(ors)),
        evidence=dict(samples=len(obs),cues=sum(x['cues'] for x in obs),files=len({x['source_sha256'] for x in obs}),
            input_tokens=sum(x['input_tokens'] for x in obs),output_tokens=sum(x['output_tokens'] for x in obs),
            actual_cost=str(sum((Decimal(x['actual_cost']) for x in obs),Decimal(0))),
            measured_from=dates[0],measured_to=dates[-1],configuration_match=match,method=METHOD,
            uncertainty='Small nonrandom samples. Sensitivity is not guaranteed bounds or a confidence interval; usage can vary by file and batch. Human review remains necessary.',
            selection_limitations=chosen['selection_limitations']))


def view(service, ident, quote_id, *, now=None):
    quote=service.estimate(ident,'revision')  # The original approval quote stays unchanged.
    if quote['quote_id']!=quote_id:raise WebError('Estimate changed. Request a fresh estimate.',409)
    plan=service.revision_plan(service.job(ident))
    result=forecast(service.root,plan,now=now)
    return dict(quote_id=quote_id,forecast=result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-samples',action='store_true',required=True)
    parser.parse_args()
    try:
        path=import_samples(Path(__file__).resolve().parents[1])
        print('Imported completed calibration evidence offline: '+path.name)
        return 0
    except (Exception,KeyboardInterrupt):
        print('Calibration import refused. Existing evidence and spending preserved; check completed records and identities.')
        return 1


if __name__=='__main__':raise SystemExit(main())
