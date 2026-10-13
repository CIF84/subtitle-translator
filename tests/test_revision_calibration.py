"""Offline calibration regressions; all calls mocked and inherited sockets blocked."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import revision_calibration as cal
import translate_srt as tr
from web.service import Jobs
import web.service as svc

ROOT=Path(tr.__file__).parent
SYNTHETIC_JOB='1'*32  # Authored test identity, never a real local job.


@pytest.fixture
def project(tmp_path,monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):return cls(2026,10,13)
    monkeypatch.setattr(svc,'date',FixedDate)
    for name in ('pricing.json','translate_srt.py','revision_experiment.py','web/service.py'):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
    raw=''.join(f'{n}\n00:00:00,000 --> 00:00:01,000\nLine {n}.\n\n' for n in range(1,817)).encode()
    target=''.join(f'{n}\n00:00:00,000 --> 00:00:01,000\nŘádek {n}.\n\n' for n in range(1,817)).encode()
    source=tmp_path/'input/web'/SYNTHETIC_JOB/'Episode.en.srt';source.parent.mkdir(parents=True);source.write_bytes(raw)
    output=tmp_path/'output/translated/Episode.cs.srt';output.parent.mkdir(parents=True);output.write_bytes(target)
    job=dict(id=SYNTHETIC_JOB,filename='Episode.en.srt',entries=816,source_sha256=tr.sha(raw),translation_file='Episode.cs.srt',translation_sha256=tr.sha(target),
        config=dict(source='en',target='cs',revision_model='gpt-5.4'),revision_complete=False)
    dest=tmp_path/'jobs'/SYNTHETIC_JOB;dest.mkdir(parents=True)
    (dest/'job.json').write_text(json.dumps(job))
    (dest/'revision-ledger.json').write_text(json.dumps(dict(version=1,identity=None,requests=[],completed_ids=[],suggestions=[],noops=[])))
    return tmp_path


def api(rows=None,**changes):
    response=NS(status='completed',incomplete_details=None,output=[],output_text=json.dumps({'corrections':rows or []},ensure_ascii=False),
        usage=NS(input_tokens=100,output_tokens=120,input_tokens_details=NS(cached_tokens=20),output_tokens_details=NS(reasoning_tokens=0)))
    for k,v in changes.items():setattr(response,k,v)
    client=Mock(api_key='fake-offline-key');client.responses.create.return_value=response
    return client


def row(ident='1',old='Řádek 1.',new='Lepší řádek 1.'):
    return dict(id=ident,existing_czech=old,proposed_czech=new,category='semantic_error',explanation='Source meaning corrected.',confidence='medium')


def execute(root,name='early',client=None):
    cal.prepare(root, job_id=SYNTHETIC_JOB);client=client or api()
    return cal.run(root,name,input_fn=lambda _: 'y',client_factory=lambda:client),client


def original_files(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and 'experiments' not in p.relative_to(root).parts}


def test_selection_preparation_identical_to_production(project):
    before=original_files(project);m=cal.prepare(project, job_id=SYNTHETIC_JOB)
    normal=object.__new__(Jobs);normal.root=project;normal.directory=project/'jobs'
    prod=normal.revision_plan(normal.job(SYNTHETIC_JOB));ours=cal.build(project)
    for n,lohi in cal.RANGES.items():
        p=ours['samples'][n];lo,hi=lohi
        assert [e.id for e in p['entries']]==list(map(str,range(lo,hi+1)))
        assert p['request']==next(p2['request'] for p2 in prod['plans'] if p2['entries'][0].id==str(lo))
        assert p['request']['reasoning']=={'effort':'none'} and p['request']['model']=='gpt-5.4'
    assert m['protocol']['samples']['early']['context_ids']==list(map(str,range(26,31)))
    assert m['protocol']['samples']['middle']['context_ids']==list(map(str,range(396,401)))+list(map(str,range(426,431)))
    assert cal.load(project)[1]['budget_usd']=='0.15' and cal.load(project)[1]['requests']==[]
    assert original_files(project)==before


def test_reprepare_preserves_ledger_and_manifest(project):
    execute(project);dest=cal.folder(project);before={p:p.read_bytes() for p in dest.rglob('*.json')}
    cal.prepare(project, job_id=SYNTHETIC_JOB)
    assert {p:p.read_bytes() for p in dest.rglob('*.json')}==before


@pytest.mark.parametrize('answer',['','n','YES PLEASE'])
def test_mandatory_approval_no_api_or_ledger_changes(project,answer):
    cal.prepare(project, job_id=SYNTHETIC_JOB);before=(cal.folder(project)/'cost-ledger.json').read_bytes();factory=Mock()
    assert cal.run(project,'early',input_fn=lambda _:answer,client_factory=factory) is None
    factory.assert_not_called();assert (cal.folder(project)/'cost-ledger.json').read_bytes()==before


def test_dry_run_no_client_or_state_write(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);before={p:p.read_bytes() for p in project.rglob('*.json')};factory=Mock()
    cal.run(project,'early',dry_run=True,client_factory=factory)
    assert {p:p.read_bytes() for p in project.rglob('*.json')}==before;factory.assert_not_called()


def test_changed_and_noop_accounting_capture_before_validation(project,monkeypatch):
    before=original_files(project);client=api([row(),row('2','Řádek 2.','Řádek 2.')]);original=cal.rev.audit_corrections
    def validate(*args,**kwargs):
        assert len(list((cal.folder(project)/'responses').glob('*.json')))==1
        return original(*args,**kwargs)
    monkeypatch.setattr(cal.rev,'audit_corrections',validate)
    result,_=execute(project,client=client);state=cal.load(project)[1]
    assert result['proposal_count']==2 and len(result['corrections'])==len(result['unchanged_proposals'])==1
    assert Decimal(state['requests'][0]['cost'])==Decimal('.002005')
    assert result['measurement']['changed_proposal_density']==.04
    assert result['measurement']['returned_proposal_density']==.08
    assert Decimal(result['measurement']['actual_cost_per_cue'])==Decimal('.002005')/25
    capture=json.loads((cal.folder(project)/result['response_capture']).read_text())
    assert capture['raw_generated_text']==client.responses.create.return_value.output_text
    assert capture['prompt_version']==svc.REVISION_VERSION and capture['usage']['output_tokens']==120
    client.responses.create.assert_called_once();client.models.retrieve.assert_not_called()
    assert original_files(project)==before


@pytest.mark.parametrize('damage',['id','duplicate','baseline','formatting','empty','fields','invalid_json','duplicate_json','incomplete'])
def test_strict_invalid_response_evidence_and_cost_preserved(project,damage):
    rows=[row()];client=api(rows)
    if damage=='id':rows[0]['id']='26'  # Context-only IDs prohibited.
    elif damage=='duplicate':rows.append(row())
    elif damage=='baseline':rows[0]['existing_czech']='wrong'
    elif damage=='formatting':rows[0]['proposed_czech']='<i>Broken'
    elif damage=='empty':rows[0]['proposed_czech']=''
    elif damage=='fields':rows[0]['timestamp']='wrong'
    client.responses.create.return_value.output_text=json.dumps({'corrections':rows})
    if damage=='invalid_json':client.responses.create.return_value.output_text='{"corrections": ['
    if damage=='duplicate_json':client.responses.create.return_value.output_text='{"corrections": [], "corrections": []}'
    if damage=='incomplete':client.responses.create.return_value.status='incomplete';client.responses.create.return_value.incomplete_details=NS(reason='max_output_tokens')
    cal.prepare(project, job_id=SYNTHETIC_JOB)
    with pytest.raises(cal.CalibrationError,match='failed'):cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    state=cal.load(project)[1];assert state['requests'][0]['status']=='settled'
    assert Decimal(state['requests'][0]['cost'])==Decimal('.002005')
    result=json.loads(next((cal.folder(project)/'results').glob('*.json')).read_text())
    assert result['status']=='failed' and result['validation_errors'] and not result['measurement']['eligible_for_projection']
    capture=json.loads((cal.folder(project)/result['response_capture']).read_text())
    assert capture['raw_generated_text']==client.responses.create.return_value.output_text
    with pytest.raises(cal.CalibrationError,match='second request'):cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    assert client.responses.create.call_count==1


def test_three_independent_invocations_and_projection(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);clients=[]
    for name in cal.RANGES:
        client=api();clients.append(client)
        cal.run(project,name,input_fn=lambda _:'y',client_factory=lambda:client)
    assert all(c.responses.create.call_count==1 for c in clients)
    result=cal.report(project)
    assert result['projection']['eligible_samples']==3 and result['projection']['reviewed_cues']==75
    assert result['projection']['changed_proposal_density']==0
    assert Decimal(result['actual_cost_usd'])==Decimal('.002005')*3
    full=cal.build(project)['protocol']['full_episode_estimate'];states=cal.load(project)[1]['requests'];specs=cal.build(project)['protocol']['samples']
    ins=300/sum(specs[r['sample']]['estimate']['input_tokens'] for r in states)
    outs=360/sum(specs[r['sample']]['estimate']['output_tokens'] for r in states)
    price=cal.build(project)['price']
    assert Decimal(result['projection']['expected_incremental_episode_cost'])==tr.cost(price,round(full['input_tokens']*ins),round(full['output_tokens']*outs))


def test_budget_guard_stops_next_sample_without_reset(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);p=cal.build(project)['samples']['early'];client=api()
    client.responses.create.return_value.usage=NS(input_tokens=p['estimate'].upper_input,output_tokens=p['estimate'].output_cap,input_tokens_details=NS(cached_tokens=0))
    cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    before=(cal.folder(project)/'cost-ledger.json').read_bytes();factory=Mock()
    with pytest.raises(tr.TranslationError,match='Budget guard'):cal.run(project,'middle',input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called();assert (cal.folder(project)/'cost-ledger.json').read_bytes()==before


@pytest.mark.parametrize('kind',['timeout','missing_usage','interrupt'])
def test_unknown_reservation_survives_rerun_and_report(project,kind):
    client=api()
    if kind=='missing_usage':client.responses.create.return_value.usage=None
    else:client.responses.create.side_effect=TimeoutError('secret') if kind=='timeout' else KeyboardInterrupt()
    cal.prepare(project, job_id=SYNTHETIC_JOB)
    with pytest.raises(cal.CalibrationError):cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    state=cal.load(project)[1];assert state['requests'][0]['status']=='unknown' and tr.totals(state)[1]>0
    before=(cal.folder(project)/'cost-ledger.json').read_bytes();cal.prepare(project, job_id=SYNTHETIC_JOB)
    assert (cal.folder(project)/'cost-ledger.json').read_bytes()==before
    summary=cal.report(project);assert summary['projection'] is None and Decimal(summary['unknown_reservations_usd'])>0


def test_credentials_redacted_from_full_response_and_results(project):
    client=api([row(new='fake-offline-key')]);cal.prepare(project, job_id=SYNTHETIC_JOB)
    with pytest.raises(cal.CalibrationError):cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    for p in cal.folder(project).rglob('*.json'):assert 'fake-offline-key' not in p.read_text()


def test_capture_failure_still_charges_and_claims_sample(project,monkeypatch):
    cal.prepare(project, job_id=SYNTHETIC_JOB);monkeypatch.setattr(cal.rev,'capture_response',Mock(side_effect=OSError('private secret')))
    with pytest.raises(cal.CalibrationError):cal.run(project,'early',input_fn=lambda _:'y',client_factory=api)
    record=cal.load(project)[1]['requests'][0]
    assert record['validation']=='response_capture_failure' and record['status']=='settled'
    assert Decimal(record['cost'])==Decimal('.002005')


@pytest.mark.parametrize('damage',['source','target','pricing','engine','ledger_deleted','cost','budget','duplicate_record'])
def test_incompatibility_fails_closed_without_api(project,damage):
    execute(project);dest=cal.folder(project);factory=Mock()
    if damage in ('source','target','pricing','engine'):
        name={'source':f'input/web/{SYNTHETIC_JOB}/Episode.en.srt','target':'output/translated/Episode.cs.srt','pricing':'pricing.json','engine':'web/service.py'}[damage]
        with (project/name).open('a') as f:f.write(' ')
    else:
        p=dest/'cost-ledger.json';state=json.loads(p.read_text())
        if damage=='ledger_deleted':p.unlink()
        else:
            if damage=='cost':state['requests'][0]['cost']='0'
            if damage=='budget':state['budget_usd']='1'
            if damage=='duplicate_record':state['requests'].append(state['requests'][0].copy())
            p.write_text(json.dumps(state))
    with pytest.raises(tr.TranslationError):cal.run(project,'middle',input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called()


def test_partial_preparation_refuses_reset(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);(cal.folder(project)/'selection.json').unlink()
    with pytest.raises(cal.CalibrationError,match='reset'):cal.prepare(project, job_id=SYNTHETIC_JOB)


def test_api_factory_failure_keeps_zero_spending(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);before=(cal.folder(project)/'cost-ledger.json').read_bytes()
    with pytest.raises(cal.CalibrationError,match='initialization'):cal.run(project,'early',input_fn=lambda _:'y',client_factory=Mock(side_effect=RuntimeError('secret-key')))
    assert (cal.folder(project)/'cost-ledger.json').read_bytes()==before


def test_one_attempt_per_sample_even_after_success(project):
    execute(project);factory=Mock()
    with pytest.raises(cal.CalibrationError,match='second request'):cal.run(project,'early',input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called()


def test_sample_claim_and_reservation_saved_before_send(project):
    cal.prepare(project, job_id=SYNTHETIC_JOB);client=api();response=client.responses.create.return_value
    def send(**request):
        state=cal.load(project)[1]
        assert len(state['requests'])==1 and state['requests'][0]['status']=='pending'
        assert state['requests'][0]['sample']=='early' and tr.totals(state)[1]>0
        return response
    client.responses.create.side_effect=send
    cal.run(project,'early',input_fn=lambda _:'y',client_factory=lambda:client)
    assert client.responses.create.call_count==1


def test_report_rejects_result_usage_tampering(project):
    execute(project);path=next((cal.folder(project)/'results').glob('*.json'));r=json.loads(path.read_text())
    r['usage']['cost']='0';path.write_text(json.dumps(r))
    with pytest.raises(cal.CalibrationError,match='mismatch'):cal.report(project)


def test_erased_ledger_records_cannot_reset_budget(project):
    execute(project);path=cal.folder(project)/'cost-ledger.json';state=json.loads(path.read_text());state['requests']=[]
    path.write_text(json.dumps(state));factory=Mock()
    with pytest.raises(cal.CalibrationError):cal.run(project,'middle',input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called()


def test_new_calibration_requires_explicit_job_without_writes(project):
    before=original_files(project)
    with pytest.raises(cal.CalibrationError,match='--job'):
        cal.prepare(project)
    assert original_files(project)==before
    assert not cal.folder(project).exists()


@pytest.mark.parametrize('job_id',['../secret','not-a-job','A'*32,'1'*31,123])
def test_invalid_job_selection_rejected_before_planning(project,job_id,monkeypatch):
    planner=Mock(side_effect=AssertionError('Invalid identity must not load production data'))
    monkeypatch.setattr(cal.ReadOnlyRevision,'revision_plan',planner)
    with pytest.raises(cal.CalibrationError,match='Invalid calibration job'):
        cal.build(project,job_id=job_id)
    planner.assert_not_called()
    assert not cal.folder(project).exists()


def test_registered_job_reused_without_hardcoded_id_or_history_changes(project):
    execute(project)
    dest=cal.folder(project)
    before={p:p.read_bytes() for p in dest.rglob('*.json')}
    plan=cal.build(project)
    assert plan['protocol']['job_id']==SYNTHETIC_JOB
    assert cal.prepare(project)['protocol']==plan['protocol']
    assert {p:p.read_bytes() for p in dest.rglob('*.json')}==before
    assert cal.load(project)[1]['requests'][0]['cost']=='0.002005'


def test_different_job_cannot_reuse_or_reset_registered_spending(project):
    execute(project);dest=cal.folder(project)
    before={p:p.read_bytes() for p in dest.rglob('*.json')}
    factory=Mock()
    with pytest.raises(cal.CalibrationError,match='differs'):
        cal.run(project,'middle',job_id='2'*32,input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called()
    assert {p:p.read_bytes() for p in dest.rglob('*.json')}==before


def test_missing_registered_job_cannot_fall_back_to_another_job(project):
    cal.prepare(project,job_id=SYNTHETIC_JOB)
    dest=cal.folder(project);path=dest/'selection.json'
    value=json.loads(path.read_text());del value['protocol']['job_id']
    path.write_text(json.dumps(value))
    ledger=dest/'cost-ledger.json';state=json.loads(ledger.read_text())
    state['selection_sha256']=tr.sha(tr.dumps(value).encode());ledger.write_text(json.dumps(state))
    with pytest.raises(cal.CalibrationError,match='Invalid registered job'):
        cal.build(project)


def test_cli_job_selection_prepare_and_saved_dry_run_are_offline(project,monkeypatch,capsys):
    monkeypatch.setattr(cal,'ROOT',project)
    factory=Mock(side_effect=AssertionError('No API client in offline commands'))
    monkeypatch.setattr(cal,'client_for',factory)
    assert cal.main(['--prepare','--job',SYNTHETIC_JOB])==0
    dest=cal.folder(project);before={p:p.read_bytes() for p in dest.rglob('*.json')}
    assert cal.main(['--sample','early','--dry-run'])==0
    factory.assert_not_called()
    assert {p:p.read_bytes() for p in dest.rglob('*.json')}==before
    assert SYNTHETIC_JOB not in capsys.readouterr().out
