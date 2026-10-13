"""Three frozen production-shaped revision samples; independent $0.15 ledger."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from pathlib import Path
import time
import uuid

import translate_srt as tr
import revision_experiment as rev
from web.service import Jobs, safe_path, REVISION_VERSION

ROOT = Path(__file__).resolve().parent
BUDGET = Decimal('0.15')
RANGES = {'early': (1, 25), 'middle': (401, 425), 'late': (751, 775)}
VERSION = 'revision-calibration-1.0'


class CalibrationError(tr.TranslationError):
    pass


class ReadOnlyRevision(Jobs):
    """Use Jobs.revision_plan without initialization, recovery or production writes.

    Calibration reviews the frozen original draft independently of any completed
    production revision IDs. Its empty in-memory state is never persisted there.
    """
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root/'jobs'

    def revision_state(self, job):
        return dict(version=1, identity=None, requests=[], completed_ids=[], suggestions=[], noops=[])


def folder(root):
    return safe_path(Path(root).resolve(), 'experiments', 'revision-calibration')


def write(path, value):
    if path.is_symlink() or path.parent.is_symlink():
        raise CalibrationError('Unsafe calibration storage.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    rev.save_json(path, value)
    path.chmod(0o600)


def estimate_data(e):
    return dict(input_tokens=e.input_tokens, output_tokens=e.output_tokens,
                upper_input_tokens=e.upper_input, output_cap=e.output_cap,
                expected_cost_usd=str(e.expected_cost), conservative_cost_usd=str(e.upper_cost))


def selected_job(root, job_id=None):
    """Select explicitly, or recover the registered job from unchanged local evidence."""
    dest = folder(root)
    if (dest/'selection.json').exists():
        manifest, _ = load(root)  # Verify registration/accounting before reusing it.
        try:
            registered = manifest['protocol']['job_id']
        except (KeyError, TypeError):
            raise CalibrationError('Invalid registered job selection; evidence preserved.') from None
        if job_id is not None and job_id != registered:
            raise CalibrationError('Job differs from the registered calibration; accounting preserved.')
        job_id = registered
    elif (dest/'cost-ledger.json').exists() or any((dest/n).exists() for n in ('responses','results')):
        raise CalibrationError('Partial calibration preparation/evidence exists. Refusing to reset accounting.')
    if job_id is None:
        raise CalibrationError('Select a local job with --job before preparing a new calibration.')
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise CalibrationError('Invalid calibration job selection.')
    return job_id


def build(root, job_id=None):
    job_id = selected_job(root, job_id)
    service = ReadOnlyRevision(root)
    job = service.job(job_id)
    if job['entries'] != 816 or any(job['config'][k] != v for k,v in
            dict(source='en',target='cs',revision_model='gpt-5.4').items()):
        raise CalibrationError('Calibration requires the 816-cue English/Czech GPT-5.4 job.')
    plan = service.revision_plan(job)  # Production request construction, unchanged.
    samples = {}
    definitions = {}
    for name,(first,last) in RANGES.items():
        ids = [str(i) for i in range(first,last+1)]
        matches = [p for p in plan['plans'] if [e.id for e in p['entries']] == ids]
        if len(matches) != 1:
            raise CalibrationError('Sample IDs do not match complete production batches.')
        sample = matches[0]
        payload = json.loads(sample['request']['input'])
        samples[name] = sample
        definitions[name] = dict(ids=ids,context_ids=[r['id'] for r in payload['read_only_context']],
            request_sha256=tr.sha(tr.dumps(sample['request']).encode()),estimate=estimate_data(sample['estimate']))
    protocol = dict(version=VERSION, job_id=job_id, model='gpt-5.4', budget_usd=str(BUDGET),
        rates={k:plan['price'][k] for k in ('input_per_million','cached_input_per_million','output_per_million')},
        production_identity=plan['identity'],samples=definitions,
        production_files={n:tr.sha((Path(root)/n).read_bytes()) for n in
                          ('translate_srt.py','revision_experiment.py','web/service.py','pricing.json')},
        tokenizer=tr.TokenCounter().label,full_episode_estimate=service.revision_estimate(plan),
        largest_production_request_allowance=str(max(p['estimate'].upper_cost for p in plan['plans'])),
        selection_method='Fixed early/middle/late contiguous production batches; not selected by known error or proposed correction. Nonrandom small sample.')
    return dict(protocol=protocol, samples=samples, price=plan['price'])


def prepare(root, job_id=None):
    plan = build(root, job_id)
    dest = folder(root)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tr.job_lock(dest/'cost-ledger.lock'):
        if (dest/'selection.json').exists():
            manifest, _ = load(root,plan)
            return manifest  # Existing selection/ledger never reset or rewritten.
        if (dest/'cost-ledger.json').exists() or any((dest/n).exists() for n in ('responses','results')):
            raise CalibrationError('Partial calibration preparation/evidence exists. Refusing to reset accounting.')
        manifest = dict(registered_at=datetime.now(timezone.utc).isoformat(),protocol=plan['protocol'])
        ledger = dict(version=1,budget_usd=str(BUDGET),selection_sha256=tr.sha(tr.dumps(manifest).encode()),requests=[])
        write(dest/'cost-ledger.json',ledger)
        write(dest/'selection.json',manifest)  # Must exist before any paid operation.
        return manifest


def load(root, plan=None):
    dest = folder(root)
    manifest = rev.read_json(dest/'selection.json')
    state = rev.read_json(dest/'cost-ledger.json')
    try:
        if plan is not None and manifest['protocol'] != plan['protocol']:
            raise ValueError
        if state['version'] != 1 or Decimal(state['budget_usd']) != BUDGET or state['selection_sha256'] != tr.sha(tr.dumps(manifest).encode()):
            raise ValueError
        if not isinstance(state['requests'],list): raise ValueError
        seen = set()
        for record in state['requests']:
            name = record['sample']
            spec = manifest['protocol']['samples'][name]
            if name in seen or record['status'] not in ('pending','unknown','settled') or record['model'] != 'gpt-5.4':
                raise ValueError
            seen.add(name)
            if not isinstance(record['id'],str) or len(record['id'])!=32 or any(c not in '0123456789abcdef' for c in record['id']): raise ValueError
            if record['protocol_hash'] != state['selection_sha256'] or record['request_sha256'] != spec['request_sha256']:
                raise ValueError
            if Decimal(record['reservation']) != Decimal(spec['estimate']['conservative_cost_usd']): raise ValueError
            if record['status']=='settled':
                i,o,c=(record[k] for k in ('input_tokens','output_tokens','cached_tokens'))
                if any(type(v) is not int or v<0 for v in (i,o,c)) or c>i: raise ValueError
                if record['rates'] != manifest['protocol']['rates'] or Decimal(record['cost']) != tr.cost(record['rates'],i,o,c): raise ValueError
        if any(not v.is_finite() or v<0 for v in tr.totals(state)[:2]): raise ValueError
        # Evidence without its ledger entry means accounting history is incomplete.
        for directory in ('responses','results'):
            if (dest/directory).exists():
                if any(p.stem not in {r['id'] for r in state['requests']} for p in (dest/directory).glob('*.json')): raise ValueError
    except (ValueError,KeyError,TypeError,InvalidOperation):
        raise CalibrationError('Calibration identity/accounting is incompatible or corrupt. Preserved; refusing to reset spending.') from None
    return manifest,state


def show(name, spec, state):
    actual,reserved,*_ = tr.totals(state)
    e = spec['estimate']
    print(f"REVISION CALIBRATION — {name}: cues {spec['ids'][0]}–{spec['ids'][-1]}\nModel: GPT-5.4; reasoning: none; requests: at most one")
    print(f"Estimated input / output tokens: {e['input_tokens']} / {e['output_tokens']}\nOutput limit: {e['output_cap']}")
    print(f"All-cues-edited estimate: ${e['expected_cost_usd']}\nConservative request allowance: ${e['conservative_cost_usd']}")
    print(f"Actual calibration spending: ${actual}\nUnsettled reservations: ${reserved}\nRemaining calibration budget: ${BUDGET-actual-reserved}\nCombined calibration limit: ${BUDGET}")
    print('Production job accounting is separate. Estimates/guard are not an absolute provider billing cap.')


def client_for(root):
    from openai import OpenAI
    key = tr.load_api_key(Path(root)/'.env')
    if not key: raise CalibrationError('Configure the API key locally; no request sent.')
    return OpenAI(api_key=key,base_url='https://api.openai.com/v1',max_retries=0,timeout=180.0)


def run(root, name, *, job_id=None, dry_run=False, input_fn=input, client_factory=None):
    plan = build(root, job_id)
    if name not in RANGES: raise CalibrationError('Unknown sample.')
    dest = folder(root)
    if dry_run:
        manifest,state = load(root,plan)
        show(name,manifest['protocol']['samples'][name],state)
        print('Dry run: zero API calls; no calibration state changes.')
        return None
    with tr.job_lock(dest/'cost-ledger.lock'):
        manifest,state = load(root,plan)
        if any(r['sample']==name for r in state['requests']):
            raise CalibrationError('This sample already has an attempted/reserved request. No second request is permitted.')
        spec = manifest['protocol']['samples'][name]
        show(name,spec,state)
        tr.budget_guard(state,BUDGET,Decimal(spec['estimate']['conservative_cost_usd']))
        if not tr.approve(input_fn):
            print('Not approved. No API calls or ledger changes.')
            return None
        client = None
        try:
            try:
                client = client_factory() if client_factory else client_for(root)
            except Exception:
                raise CalibrationError('API client initialization failed. No request sent; check local configuration. Raw error omitted.') from None
            redact = rev.credential_redactor(client)
            record = dict(id=uuid.uuid4().hex,sample=name,model='gpt-5.4',status='pending',
                reservation=spec['estimate']['conservative_cost_usd'],request_sha256=spec['request_sha256'],
                protocol_hash=state['selection_sha256'],estimate=spec['estimate'],rates=manifest['protocol']['rates'],
                approved_at=datetime.now(timezone.utc).isoformat())
            state['requests'].append(record)
            write(dest/'cost-ledger.json',state)  # Durable allowance and sample claim BEFORE dispatch.
            response = None
            audit = dict(proposal_count=None,corrections=[],unchanged_proposals=[],rejected_proposals=[],validation_errors=[])
            started = time.monotonic()
            capture = dest/'responses'/f"{record['id']}.json"
            try:
                response = client.responses.create(**plan['samples'][name]['request'])  # No access lookup, SDK retry, loop or auto-continuation.
                record.update(tr.response_metadata(response,tr.TokenCounter()))
                try:
                    rev.capture_response(capture,response,record,redact)  # Complete redacted output before validation.
                    evidence = rev.read_json(capture)
                    evidence['prompt_version'] = REVISION_VERSION
                    evidence['production_prompt_sha256'] = plan['protocol']['production_identity']['prompt_sha256']
                    write(capture,evidence)
                except (OSError,ValueError,TypeError,tr.TranslationError):
                    raise tr.InvalidBatch('Response evidence could not be saved. Usage retained.', 'response_capture_failure') from None
                tr.settle_response(record,response,plan['price'])
                write(dest/'cost-ledger.json',state)
                audit = rev.audit_corrections(response.output_text,plan['samples'][name]['cases'],redact)
                if record['response_status']!='completed' or record['refusal']:
                    audit['validation_errors'].insert(0,dict(category='response_incomplete',message='Response incomplete or refused.',details={},proposal_index=None))
                record['validation'] = audit['validation_errors'][0]['category'] if audit['validation_errors'] else 'passed'
            except (Exception,KeyboardInterrupt) as error:
                if response is not None and record['status']!='settled':
                    try: tr.settle_response(record,response,plan['price'])
                    except tr.TranslationError: pass
                if record['status']!='settled': record['status']='unknown'
                record['validation'] = error.category if isinstance(error,tr.InvalidBatch) else 'interrupted' if isinstance(error,KeyboardInterrupt) else 'request_or_usage_failure'
                audit['validation_errors'].append(dict(category=record['validation'],message='Request, usage, or evidence preservation failed.',details={},proposal_index=None))
                if response is None:
                    try: rev.capture_response(capture,None,record,redact)
                    except (OSError,ValueError,TypeError): pass
            record['elapsed_seconds'] = round(time.monotonic()-started,3)
            result = dict(version=1,sample=name,request_id=record['id'],request_sha256=record['request_sha256'],
                status='completed' if record['validation']=='passed' else 'failed',estimate=spec['estimate'],
                usage={k:record.get(k) for k in ('input_tokens','output_tokens','cached_tokens','reasoning_tokens','cost')},
                diagnostics=tr.response_metadata(response),elapsed_seconds=record['elapsed_seconds'],
                response_capture=str(capture.relative_to(dest)) if capture.exists() else None,**audit)
            result['measurement'] = dict(reviewed_cues=25,changed_proposal_density=len(audit['corrections'])/25,
                returned_proposal_density=audit['proposal_count']/25 if audit['proposal_count'] is not None else None,
                actual_cost_per_cue=str(Decimal(record['cost'])/25) if record['status']=='settled' else None,
                output_vs_all_edits_estimate=record.get('output_tokens',0)/spec['estimate']['output_tokens'] if record['status']=='settled' else None,
                eligible_for_projection=record['validation']=='passed' and record['status']=='settled')
            result = rev.safe_document(result,redact)
            write(dest/'cost-ledger.json',state)
            write(dest/'results'/f"{record['id']}.json",result)
            print(f"Response: {record.get('response_status','not_received')}; validation: {record['validation']}\nProposals: {audit['proposal_count']}; changed: {len(audit['corrections'])}; no-ops: {len(audit['unchanged_proposals'])}")
            print(f"Input / output / reasoning tokens: {record.get('input_tokens','unknown')} / {record.get('output_tokens','unknown')} / {record.get('reasoning_tokens','unknown')}\nActual cost: ${record.get('cost','unknown; reservation retained')}")
            print('Stopped. Other samples require separate invocations and explicit approval. No corrections applied.')
            if result['status']!='completed': raise CalibrationError('Sample failed; complete available evidence and spending retained. This sample cannot be retried.')
            return result
        finally:
            if client is not None:
                try: client.close()
                except Exception: pass


def report(root):
    manifest,state = load(root)  # Reports remain offline even when live pricing ages.
    dest=folder(root);records={r['id']:r for r in state['requests']}
    results=[rev.read_json(p) for p in sorted((dest/'results').glob('*.json'))]
    eligible=[]
    for result in results:
        record=records[result['request_id']]
        if result['status']=='completed' and record.get('validation')=='passed' and record['status']=='settled':
            for k in ('input_tokens','output_tokens','cached_tokens','cost'):
                if result['usage'][k]!=record[k]: raise CalibrationError('Result/ledger mismatch; evidence preserved.')
            eligible.append(result)
    actual,reserved,*_=tr.totals(state)
    projection=None
    if eligible:
        full=manifest['protocol']['full_episode_estimate'];price=manifest['protocol']['rates']
        ir=sum(r['usage']['input_tokens'] for r in eligible)/sum(r['estimate']['input_tokens'] for r in eligible)
        oratio=sum(r['usage']['output_tokens'] for r in eligible)/sum(r['estimate']['output_tokens'] for r in eligible)
        def cost_with(i,o):return str(tr.cost(price,round(full['input_tokens']*i),round(full['output_tokens']*o)))
        ins=[r['usage']['input_tokens']/r['estimate']['input_tokens'] for r in eligible]
        outs=[r['usage']['output_tokens']/r['estimate']['output_tokens'] for r in eligible]
        projection=dict(eligible_samples=len(eligible),reviewed_cues=25*len(eligible),
            changed_proposal_density=sum(len(r['corrections']) for r in eligible)/(25*len(eligible)),
            input_calibration_ratio=ir,output_calibration_ratio=oratio,
            expected_incremental_episode_cost=cost_with(ir,oratio),
            sample_ratio_sensitivity_low=cost_with(min(ins),min(outs)),
            sample_ratio_sensitivity_high=cost_with(max(ins),max(outs)),
            future_failed_request_exposure=manifest['protocol']['largest_production_request_allowance'],
            caveat='Provisional consumption extrapolation, not correction accuracy or a confidence interval. Three nonrandom batches may miss dense/long regions, failures, or context-sensitive errors. Zero proposals do not prove good translation. Cache discounts excluded from projection; failed/unknown attempts remain in calibration spending.')
    summary=dict(version=1,budget_usd=str(BUDGET),actual_cost_usd=str(actual),unknown_reservations_usd=str(reserved),
        remaining_usd=str(BUDGET-actual-reserved),attempted_samples=[r['sample'] for r in state['requests']],projection=projection)
    write(dest/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=True,indent=2))
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare',action='store_true',help='Freeze samples/identities and initialize separate zero-spend ledger offline.')
    group.add_argument('--sample',choices=RANGES,help='One sample only; spending approval always required.')
    group.add_argument('--report',action='store_true',help='Project episode consumption from saved valid results offline.')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--job', help='Local job identifier; required for a new registration, otherwise reuse its saved selection.')
    args=parser.parse_args(argv)
    if args.dry_run and not args.sample: parser.error('--dry-run requires --sample')
    if args.report and args.job: parser.error('--report uses the saved selection; omit --job')
    try:
        if args.prepare:
            m=prepare(ROOT, job_id=args.job)
            print('Samples registered offline; separate $0.15 ledger; zero API calls.')
            for n,s in m['protocol']['samples'].items():print(n,s['ids'][0],s['ids'][-1])
        elif args.report: report(ROOT)
        else: run(ROOT,args.sample,job_id=args.job,dry_run=args.dry_run)
        return 0
    except (tr.TranslationError,OSError,ValueError,KeyError):
        # Safe operational messages only; never include raw SDK errors or paths.
        import sys
        error=sys.exc_info()[1]
        print('Error: '+str(error) if isinstance(error,tr.TranslationError) else 'Error: Local calibration operation failed. Evidence/accounting preserved.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
