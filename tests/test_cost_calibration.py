"""Optional forecast cannot alter request caps, approval or spending enforcement."""
import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
import revision_calibration as samples
import translate_srt as tr
from web import calibration
from web.app import create_app
from web.service import Jobs,WebError
from test_revision_calibration import project,api,SYNTHETIC_JOB
from test_web import Deferred


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def collect(root,count=3):
    samples.prepare(root, job_id=SYNTHETIC_JOB)
    for name in list(samples.RANGES)[:count]:
        samples.run(root,name,input_fn=lambda _:'y',client_factory=api)
    return calibration.import_samples(root)


def ready(root):
    path=root/'jobs'/SYNTHETIC_JOB/'job.json';j=json.loads(path.read_text())
    j.update(status='translated',created=0,error=None,revision_complete=False,suggestions=[],decisions={},final_file=None,
             translation_checkpoint_created=False)
    j['config'].update(translation_model='gpt-5.4-mini',revision_enabled=True,max_cost='1.00')
    path.write_text(json.dumps(j))
    return j


def test_import_is_separate_authoritative_and_idempotent(project):
    samples.prepare(project, job_id=SYNTHETIC_JOB)
    for n in samples.RANGES:samples.run(project,n,input_fn=lambda _:'y',client_factory=api)
    before=hashes(project);path=calibration.import_samples(project)
    for n,h in before.items():assert hashes(project)[n]==h
    record=json.loads(path.read_text())
    assert record['method']==calibration.METHOD
    assert len(record['observations'])==3 and sum(o['cues'] for o in record['observations'])==75
    assert sum(Decimal(o['actual_cost']) for o in record['observations'])==Decimal('.006015')
    assert all(o['measured_at'] and o['historical_rates'] for o in record['observations'])
    assert not any('summary' in n for n in record['evidence_files'])
    after=hashes(project);assert calibration.import_samples(project)==path and hashes(project)==after


def test_pooled_separate_token_estimation_and_new_file_match(project):
    collect(project);plan=samples.build(project);actual=calibration.forecast(project,dict(identity=plan['protocol']['production_identity'],plans=list(plan['samples'].values()),price=plan['price']))
    assert actual['kind']=='calibrated' and actual['evidence']['samples']==3 and actual['evidence']['cues']==75
    # Forecast the three measured requests: ratios reproduce their actual token totals,
    # rather than multiplying cost by zero proposed corrections in these mock samples.
    assert actual['input_tokens']==300 and actual['output_tokens']==360
    assert Decimal(actual['expected_cost'])==tr.cost(plan['price'],300,360)
    changed=copy.deepcopy(plan['protocol']['production_identity']);changed['source_sha256']='different-file';changed['target_sha256']='different-draft'
    independent=dict(identity=changed,plans=list(plan['samples'].values()),price=plan['price'])
    assert calibration.forecast(project,independent)['kind']=='calibrated'


@pytest.mark.parametrize('key',['model','source','target','prompt_version','prompt_sha256','schema_version','batch_size','context','reasoning'])
def test_configuration_mismatch_falls_back(project,key):
    collect(project);s=samples.build(project);p=dict(identity=copy.deepcopy(s['protocol']['production_identity']),plans=list(s['samples'].values()),price=s['price'])
    p['identity'][key]='changed'
    assert calibration.forecast(project,p)['kind']=='heuristic'


def test_tokenizer_mismatch_falls_back(project,monkeypatch):
    collect(project);p=samples.ReadOnlyRevision(project).revision_plan(samples.ReadOnlyRevision(project).job(SYNTHETIC_JOB))
    real=tr.TokenCounter
    class Different(real):
        def __init__(self):super().__init__();self.label='other tokenizer'
    monkeypatch.setattr(tr,'TokenCounter',Different)
    assert calibration.forecast(project,p)['kind']=='heuristic'


@pytest.mark.parametrize('count',[0,1,2])
def test_missing_or_insufficient_evidence_falls_back(project,count):
    if count:collect(project,count)
    s=samples.ReadOnlyRevision(project);plan=s.revision_plan(s.job(SYNTHETIC_JOB))
    result=calibration.forecast(project,plan)
    assert result['kind']=='heuristic' and result['evidence'] is None


def test_stale_and_future_evidence_falls_back(project):
    collect(project);s=samples.ReadOnlyRevision(project);p=s.revision_plan(s.job(SYNTHETIC_JOB));now=datetime.now(timezone.utc)
    assert calibration.forecast(project,p,now=now+timedelta(days=91))['kind']=='heuristic'
    assert calibration.forecast(project,p,now=now-timedelta(days=1))['kind']=='heuristic'


def test_new_pricing_reprices_tokens_not_historical_cost(project):
    path=collect(project);before=hashes(project);s=samples.ReadOnlyRevision(project);p=s.revision_plan(s.job(SYNTHETIC_JOB))
    base=calibration.forecast(project,p);p['price']=copy.deepcopy(p['price'])
    p['price']['input_per_million']='5.00';p['price']['output_per_million']='30.00'
    revised=calibration.forecast(project,p)
    assert Decimal(revised['expected_cost'])==Decimal(base['expected_cost'])*2
    assert revised['evidence']['actual_cost']==base['evidence']['actual_cost']
    assert hashes(project)==before and path.exists()


@pytest.mark.parametrize('damage',['result','capture','ledger','index','missing_capture','symlink_capture','derived_token_forgery'])
def test_invalid_evidence_falls_back_without_repair(project,damage):
    index=collect(project);origin=samples.folder(project)
    if damage=='derived_token_forgery':
        r=json.loads(index.read_text());r['observations'][0]['input_tokens']=1000
        r['observations'][0]['actual_cost']=str(tr.cost(r['observations'][0]['historical_rates'],1000,r['observations'][0]['output_tokens'],r['observations'][0]['cached_tokens']))
        index.unlink();index.with_name(tr.sha(tr.dumps(r).encode())+'.json').write_text(json.dumps(r))
    elif damage=='index':index.write_text('{}')
    else:
        path=next((origin/'results').glob('*.json')) if damage=='result' else origin/'cost-ledger.json' if damage=='ledger' else next((origin/'responses').glob('*.json'))
        if damage=='missing_capture':path.unlink()
        elif damage=='symlink_capture':
            content=path.read_text();backup=project/'backup.json';backup.write_text(content);path.unlink();path.symlink_to(backup)
        else:path.write_text(path.read_text()+' ')
    before=hashes(project);s=samples.ReadOnlyRevision(project);p=s.revision_plan(s.job(SYNTHETIC_JOB))
    assert calibration.forecast(project,p)['kind']=='heuristic' and hashes(project)==before


def test_invalid_completed_record_cannot_be_imported(project):
    samples.prepare(project, job_id=SYNTHETIC_JOB)
    for n in samples.RANGES:samples.run(project,n,input_fn=lambda _:'y',client_factory=api)
    path=next((samples.folder(project)/'results').glob('*.json'));r=json.loads(path.read_text());r['usage']['cost']='0';path.write_text(json.dumps(r))
    with pytest.raises(WebError):calibration.import_samples(project)
    assert not (project/'calibrations/revision').exists()


def test_stale_summary_ignored(project):
    collect(project);(samples.folder(project)/'summary.json').write_text('not JSON and incorrect spending')
    s=samples.ReadOnlyRevision(project);p=s.revision_plan(s.job(SYNTHETIC_JOB));before=hashes(project)
    assert calibration.forecast(project,p)['kind']=='calibrated' and hashes(project)==before


def test_conservative_quote_guard_approval_and_resume_unchanged(project):
    collect(project);ready(project);factory=Mock();j=Jobs(project,factory,Deferred());quote=j.estimate(SYNTHETIC_JOB,'revision')
    before=hashes(project);view=calibration.view(j,SYNTHETIC_JOB,quote['quote_id'])
    assert view['forecast']['kind']=='calibrated'
    assert j.estimate(SYNTHETIC_JOB,'revision')==quote and hashes(project)==before
    p=j.revision_plan(j.job(SYNTHETIC_JOB))
    assert Decimal(quote['conservative_cost'])==sum(x['estimate'].upper_cost for x in p['plans'])
    assert Decimal(quote['next_request_cost'])==p['plans'][0]['estimate'].upper_cost
    with pytest.raises(WebError,match='approved'):j.start(SYNTHETIC_JOB,'revision',False,quote['quote_id'])
    job=ready(project);job['config']['max_cost']='0.001';(j.folder(SYNTHETIC_JOB)/'job.json').write_text(json.dumps(job))
    small=j.estimate(SYNTHETIC_JOB,'revision');calibration.view(j,SYNTHETIC_JOB,small['quote_id'])
    with pytest.raises(WebError,match='budget'):j.start(SYNTHETIC_JOB,'revision',True,small['quote_id'])
    factory.assert_not_called();assert j.revision_state(j.job(SYNTHETIC_JOB))['requests']==[]


def test_route_readonly_matching_quote_no_api_and_stale_quote_rejection(project):
    collect(project);ready(project);factory=Mock();app=create_app(project,factory,Deferred());j=app.state.jobs
    with TestClient(app,base_url='http://127.0.0.1:8000') as c:
        q=j.estimate(SYNTHETIC_JOB,'revision');before=hashes(project)
        response=c.get(f"/api/jobs/{SYNTHETIC_JOB}/revision-cost/{q['quote_id']}")
        assert response.status_code==200 and response.json()['forecast']['kind']=='calibrated'
        assert response.json()['quote_id']==q['quote_id'] and hashes(project)==before
        assert c.get(f"/api/jobs/{SYNTHETIC_JOB}/revision-cost/wrong").status_code==409
        assert c.get(f"/api/jobs/invalid/revision-cost/{q['quote_id']}").status_code==404
        assert not any('evidence_files' in k for k in response.json())
    factory.assert_not_called()
