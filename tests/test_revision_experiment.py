"""WP2 tests use authored synthetic data and mocks; inherited fixture forbids sockets."""
import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import revision_experiment as rev
import translate_srt as tr
from synthetic_revision import build_project

ROOT = Path(rev.__file__).parent


@pytest.fixture
def project(tmp_path,monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):return cls(2026,10,11)
    monkeypatch.setattr(rev,'date',FixedDate)
    build_project(tmp_path, ROOT)
    return tmp_path


@pytest.fixture
def cases(project):
    return rev.load_benchmark(project)[0]['cases']


def correction(cases, ident='1', text='[Alex]\n<i>Domluv videohovor.</i>'):
    c = next(c for c in cases if c['id']==ident)
    return {'id':ident,'existing_czech':c['baseline_czech'],'proposed_czech':text,
            'category':'semantic_error','explanation':'Preserve the request to arrange a video call.','confidence':'high'}


def client_for(rows=None, **changes):
    response = NS(status='completed',incomplete_details=None,output=[],
                  output_text=json.dumps({'corrections':rows or []},ensure_ascii=False),
                  usage=NS(input_tokens=100,output_tokens=120,input_tokens_details=NS(cached_tokens=20),
                           output_tokens_details=NS(reasoning_tokens=0)))
    for key,value in changes.items(): setattr(response,key,value)
    client = Mock(api_key='fake-offline-key')
    client.responses.create.return_value = response
    return client


def execute(project, choice='mini', client=None, **kwargs):
    client = client or client_for()
    result = rev.run_experiment(project,choice,input_fn=lambda _: 'y',client_factory=lambda:client,**kwargs)
    return result,client


def ledger(project):
    return rev.read_json(project/'experiments/revision/cost-ledger.json')


def test_benchmark_exact_source_identity(project):
    b,en,cs=rev.load_benchmark(project)
    assert len(b['cases'])==52 and len(en)==len(cs)==816
    assert b['cases'][0]['id']=='1' and b['cases'][-1]['id']=='803'


@pytest.mark.parametrize('damage',['source','target','engine','context','duplicate'])
def test_benchmark_rejects_incompatibility(project,damage):
    path=project/'benchmarks/mobland-v1-baseline.json';b=rev.read_json(path)
    if damage in ['source','target','engine']:
        name=b['provenance']['english_file' if damage=='source' else 'czech_file'] if damage!='engine' else 'translate_srt.py'
        with (project/name).open('a') as f:f.write('damaged')
    else:
        if damage=='context':b['cases'][0]['context']['after'][0]['english']='incorrect context'
        else:b['cases'][1]['id']='1'
        rev.save_json(path,b)
    with pytest.raises(rev.ExperimentError):rev.load_benchmark(project)


def test_reference_composition_and_absent_ground_truth(project):
    b,*_=rev.load_benchmark(project);r=rev.load_references(project,b)
    assert len(r['high_confidence_ids'])==13 and len(r['control_ids'])==9 and len(r['unresolved_ids'])==5
    assert sum(c['reference_status']=='user_confirmed_error' for c in r['cases'])==1
    assert all(c['numeric_ground_truth'] is None for c in r['cases'])
    assert set(r['high_confidence_ids']).isdisjoint(r['control_ids'])
    assert '438' in r['high_confidence_ids'] and '741' not in r['control_ids']
    assert '666' not in r['high_confidence_ids']


@pytest.mark.parametrize('damage',['fake_scores','too_few_controls','bad_status','wrong_fixture','no_terminology'])
def test_invalid_references(project,damage):
    path=project/'benchmarks/mobland-v1-revision-references.json';r=rev.read_json(path)
    if damage=='fake_scores':r['cases'][0]['numeric_ground_truth']=4
    elif damage=='too_few_controls':r['control_ids']=r['control_ids'][:4]
    elif damage=='bad_status':r['cases'][0]['reference_status']='automatically_confirmed'
    elif damage=='wrong_fixture':r['benchmark_sha256']='wrong'
    else:
        for c in r['cases']:c['applicability']['terminology']=False
    rev.save_json(path,r)
    with pytest.raises(rev.ExperimentError):rev.load_references(project,rev.load_benchmark(project)[0])


def test_models_differ_only_in_model_and_pricing(project):
    mini=rev.build_plan(project,'mini');strong=rev.build_plan(project,'strong')
    a=copy.deepcopy(mini['request']);b=copy.deepcopy(strong['request'])
    assert a.pop('model')=='gpt-5.4-mini' and b.pop('model')=='gpt-5.4'
    assert a==b and mini['protocol_hash']==strong['protocol_hash']
    payload=json.loads(a['input'])
    assert len(payload['review'])==52 and payload['read_only_context']
    assert 'explicit_speaker_labels' in payload['review'][0]
    assert all('acceptance_criterion' not in row and 'proposed_correction' not in row for row in payload['review'])
    assert 'ground_truth' not in a['input'] and 'human_scores' not in a['input']
    assert a['reasoning']=={'effort':'none'}
    assert a['text']['format']['schema']['properties']['corrections']['maxItems']==52


def test_cost_estimation_includes_full_request_and_cap(project):
    plan=rev.build_plan(project,'mini');e=plan['estimate'];p=plan['price']
    assert e['input_tokens']>10000 and e['output_tokens']>1000
    assert e['output_cap']==8192
    assert Decimal(e['conservative_cost_usd'])==tr.cost(p,e['upper_input_tokens'],8192)
    assert Decimal(e['expected_cost_usd'])<Decimal(e['conservative_cost_usd'])


@pytest.mark.parametrize('cap',[0,512,1024,200000])
def test_unsafe_output_cap(project,cap):
    with pytest.raises(rev.ExperimentError):rev.build_plan(project,'mini',cap)


def test_unknown_or_changed_price_fails_closed(project):
    path=project/'pricing.json';p=rev.read_json(path);p['models']['gpt-5.4-mini']['output_per_million']='0.01'
    rev.save_json(path,p)
    with pytest.raises(rev.ExperimentError):rev.build_plan(project,'mini')


def test_expired_pricing_fails_closed(project):
    path=project/'pricing.json';p=rev.read_json(path);p['last_verified']='2026-09-01'
    rev.save_json(path,p)
    with pytest.raises(rev.ExperimentError,match='older than 30'):rev.build_plan(project,'mini')


def test_parse_only_corrections_and_local_timestamp(cases):
    rows=rev.parse_corrections(json.dumps({'corrections':[correction(cases)]}),cases)
    assert len(rows)==1 and rows[0]['id']=='1' and rows[0]['timestamp']==cases[0]['timestamp']
    assert rev.parse_corrections('{"corrections":[]}',cases)==[]


@pytest.mark.parametrize('damage',['invalid_id','context_id','duplicate','baseline','empty','tags','speaker','extra','confidence','category','long_explanation','numeric_id'])
def test_reject_invalid_corrections(cases,damage):
    row=correction(cases);rows=[row]
    if damage=='invalid_id':row['id']='9999'
    elif damage=='context_id':row['id']='2'
    elif damage=='duplicate':rows.append(copy.deepcopy(row))
    elif damage=='baseline':row['existing_czech']='different text'
    elif damage=='empty':row['proposed_czech']=''
    elif damage=='tags':row['proposed_czech']='[Alex]\n<i>Domluv videohovor.'
    elif damage=='speaker':row['proposed_czech']='[Blair]\n<i>Domluv videohovor.</i>'
    elif damage=='extra':row['timestamp']='new timestamp'
    elif damage=='confidence':row['confidence']='certain'
    elif damage=='category':row['category']='unknown'
    elif damage=='long_explanation':row['explanation']='x'*201
    else:row['id']=1
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(json.dumps({'corrections':rows}),cases)


@pytest.mark.parametrize('raw',['not JSON','{"entries":[]}','{"corrections":[],"corrections":[]}','{"corrections":{}}'])
def test_invalid_json_schema(cases,raw):
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(raw,cases)


def test_invalid_explanation_unicode(cases):
    row=correction(cases);row['explanation']='\ud800'
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(json.dumps({'corrections':[row]}),cases)


def test_sdh_and_multispeaker_preservation(cases):
    row=correction(cases,'143','- Připravíš šťávu z malin?\n- [robot míchá ovoce]')
    assert rev.parse_corrections(json.dumps({'corrections':[row]}),cases)[0]['id']=='143'
    row['proposed_czech']='Připravíš šťávu z malin? [robot míchá ovoce]'
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(json.dumps({'corrections':[row]}),cases)


@pytest.mark.parametrize('answer',['','n','N','no','yes'])
def test_approval_defaults_no(project,answer):
    factory=Mock(side_effect=AssertionError('Client must not be created'))
    assert rev.run_experiment(project,'mini',input_fn=lambda _:answer,client_factory=factory) is None
    factory.assert_not_called()
    assert not (project/'experiments/revision/cost-ledger.json').exists()


def test_approval_eof_no_calls(project):
    factory=Mock()
    def eof(_):raise EOFError
    assert rev.run_experiment(project,'mini',input_fn=eof,client_factory=factory) is None
    factory.assert_not_called()


def test_dry_run_zero_calls_zero_writes(project):
    factory=Mock(side_effect=AssertionError('No client during dry run'))
    assert rev.run_experiment(project,'strong',dry_run=True,client_factory=factory)['output_cap']==8192
    factory.assert_not_called()
    assert not (project/'experiments').exists()


def test_budget_guard_before_approval(project):
    factory=Mock();approval=Mock(return_value='y')
    with pytest.raises(rev.ExperimentError,match='budget'):
        rev.run_experiment(project,'mini',max_cost=Decimal('.01'),client_factory=factory,input_fn=approval)
    factory.assert_not_called();approval.assert_not_called()


def test_each_run_is_independent_and_preserves_cumulative_cost(project,cases):
    first,client=execute(project,client=client_for([correction(cases)]))
    client.responses.create.assert_called_once();client.models.retrieve.assert_called_once_with('gpt-5.4-mini')
    assert not (project/'experiments/revision/strong-results.json').exists()
    old=copy.deepcopy(ledger(project)['requests'])
    second,other=execute(project,'strong')
    other.responses.create.assert_called_once();assert ledger(project)['requests'][:1]==old
    assert tr.totals(ledger(project))[0]==Decimal('.0026065')
    assert first['protocol_hash']==second['protocol_hash']
    assert (project/'experiments/revision/attempts'/f"{first['request_id']}.json").exists()


def test_source_completed_subtitles_and_benchmark_never_changed(project,cases):
    b,*_=rev.load_benchmark(project)
    files=['translate_srt.py','benchmarks/mobland-v1-baseline.json',b['provenance']['english_file'],b['provenance']['czech_file']]
    before={name:rev.digest(project/name) for name in files}
    execute(project,client=client_for([correction(cases)]))
    execute(project,'strong')
    assert {name:rev.digest(project/name) for name in files}==before


def test_existing_budget_persists_not_reset_on_second_run(project):
    execute(project,max_cost=Decimal('.75'))
    execute(project,'strong')
    assert ledger(project)['budget_usd']=='0.75'


def test_exhausted_previous_spending_blocks_new_model(project):
    execute(project)
    path=project/'experiments/revision/cost-ledger.json';state=ledger(project);r=state['requests'][0]
    r.update(input_tokens=1_333_300,output_tokens=0,cached_tokens=0,cost='0.999975')
    rev.save_json(path,state)
    factory=Mock()
    with pytest.raises(rev.ExperimentError,match='budget'):
        rev.run_experiment(project,'strong',client_factory=factory,input_fn=lambda _:'y')
    factory.assert_not_called();assert ledger(project)['requests'][0]['cost']=='0.999975'


def test_completed_result_is_not_overwritten_or_recharged(project):
    execute(project);before=ledger(project)
    with pytest.raises(rev.ExperimentError,match='already completed'):execute(project)
    assert ledger(project)==before


def test_unmatched_second_model_refused(project):
    execute(project)
    with pytest.raises(rev.ExperimentError,match='settings changed'):execute(project,'strong',output_cap=10000)


def test_invalid_response_is_charged_and_kept_separate(project,cases):
    client=client_for([correction(cases)]);client.responses.create.return_value.output_text='truncated secret-like dialogue'
    with pytest.raises(rev.ExperimentError,match='invalid_json'):execute(project,client=client)
    state=ledger(project)
    assert state['requests'][0]['status']=='settled' and tr.totals(state)[0]==Decimal('.0006015')
    assert rev.read_json(project/'experiments/revision/mini-latest-results.json')['corrections']==[]
    assert 'truncated secret-like dialogue' not in (project/'experiments/revision/mini-latest-results.json').read_text()


def test_failed_attempt_manual_retry_requires_new_approval_retains_cost(project):
    with pytest.raises(rev.ExperimentError):execute(project,client=client_for(status='incomplete',incomplete_details=NS(reason='max_output_tokens')))
    execute(project)
    assert len(ledger(project)['requests'])==2 and tr.totals(ledger(project))[0]==Decimal('.0012030')


@pytest.mark.parametrize('failure',['timeout','missing_usage','invalid_usage'])
def test_uncertain_usage_keeps_reservation(project,failure):
    client=client_for()
    if failure=='timeout':client.responses.create.side_effect=TimeoutError('Authorization Bearer actual-secret')
    elif failure=='missing_usage':client.responses.create.return_value.usage=None
    else:client.responses.create.return_value.usage.input_tokens=-1
    with pytest.raises(rev.ExperimentError):execute(project,client=client)
    actual,reserved,*_=tr.totals(ledger(project))
    assert actual==0 and reserved>0 and ledger(project)['requests'][0]['status']=='unknown'
    assert 'actual-secret' not in (project/'experiments/revision/cost-ledger.json').read_text()


def test_unavailable_model_does_not_dispatch_or_spend(project):
    client=client_for();client.models.retrieve.side_effect=RuntimeError('secret from SDK')
    with pytest.raises(rev.ExperimentError,match='access check'):execute(project,'strong',client=client)
    client.responses.create.assert_not_called()
    assert not (project/'experiments/revision/cost-ledger.json').exists()


def test_corrupt_ledger_fails_closed(project):
    path=project/'experiments/revision/cost-ledger.json';path.parent.mkdir(parents=True);path.write_text('not json')
    with pytest.raises(rev.ExperimentError):execute(project)
    assert path.read_text()=='not json'


def test_cost_ledger_cannot_silently_drop_charges(project):
    execute(project);path=project/'experiments/revision/cost-ledger.json';state=ledger(project)
    state['requests'][0]['cost']='0';rev.save_json(path,state)
    with pytest.raises(rev.ExperimentError,match='ledger'):execute(project,'strong')


def test_comparison_and_review_template_remain_unscored(project,cases):
    result,_=execute(project,client=client_for([correction(cases)]))
    report=rev.comparison(project)
    assert 'pending complete independent ratings' in report and '62.5' not in report
    assert 'Domluv videohovor.' in report and 'strong' in report
    assert 'not_run' in report and 'Actual experiment spending' in report
    b,*_=rev.load_benchmark(project);template=rev.review_template(rev.load_references(project,b))
    assert template['reviewer'] is None
    assert all(v is None for c in template['cases'] for v in c['baseline_scores'].values())


def test_human_review_requires_identity_and_notes(project):
    execute(project);b,*_=rev.load_benchmark(project);reviews=rev.review_template(rev.load_references(project,b))
    reviews['cases'][0]['mini']['outcome']='corrected';path=project/'reviews.json';rev.save_json(path,reviews)
    with pytest.raises(rev.ExperimentError,match='review'):rev.comparison(project,path)


def test_contextual_reference_preserves_continuation(project,cases):
    b,en,cs=rev.load_benchmark(project);p=rev.revision_payload(b,en,cs)
    ctx={c['id']:c for c in p['read_only_context']}
    assert 'Test cue 744:' in ctx['744']['english']
    assert '557' in ctx and 'Test cue 557:' in ctx['557']['english']
    assert not set(c['id'] for c in p['review']) & set(ctx)


def test_valid_wrapping_and_source_order(cases):
    first=correction(cases)
    later=correction(cases,'595','[zasměje se]')
    first['proposed_czech']='[Alex] <i>Domluv\nvideohovor.</i>'
    rows=rev.parse_corrections(json.dumps({'corrections':[later,first]}),cases)
    assert [c['id'] for c in rows]==['1','595']


def test_secret_like_valid_content_is_never_persisted(project,cases):
    row=correction(cases);row['explanation']='fake-offline-key'
    with pytest.raises(rev.ExperimentError,match='sensitive_content'):execute(project,client=client_for([row]))
    assert 'fake-offline-key' not in (project/'experiments/revision/mini-latest-results.json').read_text()


def test_weighted_framework_with_synthetic_test_ratings_only(project):
    """Synthetic ratings exist only in tmp_path to test arithmetic, never in references."""
    result,_=execute(project)
    b,*_=rev.load_benchmark(project);refs=rev.load_references(project,b)
    reviews=rev.review_template(refs);reviews['reviewer']='Synthetic arithmetic test, not an actual reviewer'
    reviews['request_ids']['mini']=result['request_id']
    for c,r in zip(reviews['cases'],refs['cases']):
        c['reference_adjudication']='accepted'
        c['mini']['outcome']='preserved';c['mini']['notes']='Synthetic test value, not a real quality judgment.'
        for k in rev.WEIGHTS:
            if r['applicability'][k]:c['baseline_scores'][k]=c['mini']['scores'][k]=4
    path=project/'synthetic-reviews.json';rev.save_json(path,reviews)
    report=rev.comparison(project,path)
    assert 'baseline 100.00/100; mini 100.00/100' in report
    assert '47' in report and 'strong' in report


def test_review_bound_to_exact_result_request(project):
    execute(project)
    b,*_=rev.load_benchmark(project);r=rev.review_template(rev.load_references(project,b))
    r['reviewer']='Synthetic validation test';r['request_ids']['mini']='wrong'
    r['cases'][0]['mini'].update(outcome='preserved',notes='Test only')
    path=project/'reviews.json';rev.save_json(path,r)
    with pytest.raises(rev.ExperimentError):rev.comparison(project,path)


def test_official_sdk_serialization_with_mocktransport_only(project):
    import openai,httpx
    received=[]
    def handler(request):
        if request.method=='GET':
            return httpx.Response(200,json={'id':'gpt-5.4-mini','object':'model','created':0,'owned_by':'openai'})
        received.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'resp_mock','object':'response','created_at':0,'status':'completed',
            'model':'gpt-5.4-mini','output':[{'id':'msg_mock','type':'message','status':'completed','role':'assistant',
            'content':[{'type':'output_text','text':'{"corrections":[]}','annotations':[]}]}],
            'usage':{'input_tokens':100,'output_tokens':120,'total_tokens':220,'input_tokens_details':{'cached_tokens':20},'output_tokens_details':{'reasoning_tokens':0}}})
    client=openai.OpenAI(api_key='offline-fake',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    result,_=execute(project,client=client)
    assert result['status']=='completed' and len(received)==1
    assert received[0]['text']['format']['strict'] is True and received[0]['reasoning']['effort']=='none'


def noop(cases, ident='675'):
    baseline=next(c['baseline_czech'] for c in cases if c['id']==ident)
    return correction(cases,ident,baseline)


def saved_response(project,result):
    return rev.read_json(project/'experiments/revision'/result['response_capture'])


def test_mixed_changed_and_unchanged_proposals(cases):
    rows=[correction(cases,'595','[smích]'),noop(cases),correction(cases)]
    audit=rev.audit_corrections(json.dumps({'corrections':rows}),cases)
    assert audit['proposal_count']==3 and audit['validation_errors']==[]
    assert [c['id'] for c in audit['corrections']]==['1','595']
    assert audit['unchanged_proposals'][0]['id']=='675'
    assert audit['unchanged_proposals'][0]['reason']=='unchanged_after_outer_whitespace_trim'
    assert audit['unchanged_proposals'][0]['proposal']==rows[1]
    assert [c['id'] for c in rev.parse_corrections(json.dumps({'corrections':rows}),cases)]==['1','595']


def test_only_unchanged_is_a_successful_empty_revision(project,cases):
    result,client=execute(project,client=client_for([noop(cases)]))
    assert result['status']=='completed' and result['corrections']==[]
    assert result['unchanged_proposals'][0]['id']=='675'
    assert result['proposal_count']==1 and result['rejected_proposals']==[]
    client.responses.create.assert_called_once()
    assert tr.totals(ledger(project))[0]==Decimal('.0006015')


def test_outer_whitespace_noop_does_not_rewrite_baseline(cases):
    row=noop(cases);row['proposed_czech']='  '+row['proposed_czech']+'  '
    audit=rev.audit_corrections(json.dumps({'corrections':[row]}),cases)
    assert not audit['validation_errors'] and audit['corrections']==[]
    assert len(audit['unchanged_proposals'])==1


@pytest.mark.parametrize('damage',['baseline','invalid_id','duplicate','formatting','speaker','extra_field'])
def test_unchanged_does_not_bypass_structural_validation(cases,damage):
    cases=copy.deepcopy(cases);row=noop(cases,'1');rows=[row]
    if damage=='baseline':row['existing_czech']='wrong baseline'
    elif damage=='invalid_id':row['id']='9999'
    elif damage=='duplicate':rows.append(copy.deepcopy(row))
    elif damage=='extra_field':row['timestamp']='invented'
    else:
        c=next(c for c in cases if c['id']=='1')
        bad='[Alex]\n<i>Domluv krátký videohovor.' if damage=='formatting' else '[Blair]\n<i>Domluv krátký videohovor.</i>'
        c['baseline_czech']=row['existing_czech']=row['proposed_czech']=bad
    audit=rev.audit_corrections(json.dumps({'corrections':rows}),cases)
    assert audit['validation_errors'] and audit['unchanged_proposals']==[] and audit['corrections']==[]
    assert len(audit['rejected_proposals'])==len(rows)
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(json.dumps({'corrections':rows}),cases)


def test_invalid_rows_reject_response_but_preserve_valid_proposals_and_all_diagnostics(project,cases,capsys):
    bad=correction(cases,'595','[smích');wrong=correction(cases,'7','[Blair přes telefon]\n<i>Pošlu výkres zítra.</i>')
    wrong['existing_czech']='wrong baseline'
    duplicate=noop(cases,'3');unknown=correction(cases);unknown['id']='9999'
    rows=[correction(cases),noop(cases),bad,duplicate,copy.deepcopy(duplicate),unknown,wrong]
    client=client_for(rows);raw=client.responses.create.return_value.output_text
    with pytest.raises(rev.ExperimentError):execute(project,client=client)
    result=rev.load_result(project/'experiments/revision','mini')
    assert result['status']=='failed' and [c['id'] for c in result['corrections']]==['1']
    assert result['unchanged_proposals'][0]['id']=='675'
    assert len(result['rejected_proposals'])==5 and len(result['validation_errors'])==5
    assert {e['proposal_index'] for e in result['validation_errors']}=={3,4,5,6,7}
    assert any(e['category']=='baseline_mismatch' for e in result['validation_errors'])
    assert any(e['category']=='identifiers' for e in result['validation_errors'])
    assert saved_response(project,result)['raw_generated_text']==raw
    assert tr.totals(ledger(project))[0]==Decimal('.0006015')
    assert 'Domluv videohovor.' not in capsys.readouterr().out


def test_response_saved_before_validation_with_private_permissions(project,cases,monkeypatch):
    original=rev.audit_corrections
    client=client_for([correction(cases),noop(cases)])
    def inspect(raw,*args):
        paths=list((project/'experiments/revision/responses').glob('*.json'))
        assert len(paths)==1 and rev.read_json(paths[0])['raw_generated_text']==raw
        assert paths[0].stat().st_mode & 0o777==0o600
        assert paths[0].parent.stat().st_mode & 0o777==0o700
        return original(raw,*args)
    monkeypatch.setattr(rev,'audit_corrections',inspect)
    result,_=execute(project,client=client)
    assert result['status']=='completed' and len(result['corrections'])==1
    assert saved_response(project,result)['output_limit']==8192
    assert saved_response(project,result)['request_schema_version']==rev.SCHEMA_VERSION


@pytest.mark.parametrize('raw',['not JSON','{"corrections":[','{"corrections":[],"corrections":[]}','{"corrections":null}','{"corrections":[NaN]}'])
def test_malformed_responses_retain_complete_evidence(project,raw):
    with pytest.raises(rev.ExperimentError):execute(project,client=client_for(output_text=raw))
    result=rev.load_result(project/'experiments/revision','mini')
    assert result['status']=='failed' and result['validation_errors']
    assert saved_response(project,result)['raw_generated_text']==raw
    assert tr.totals(ledger(project))[0]==Decimal('.0006015')


def test_invalid_root_preserves_valid_rows_as_evidence(cases):
    audit=rev.audit_corrections(json.dumps({'corrections':[correction(cases)],'extra':True}),cases)
    assert audit['validation_errors'][0]['category']=='schema_mismatch'
    assert [c['id'] for c in audit['corrections']]==['1']
    with pytest.raises(tr.InvalidBatch):rev.parse_corrections(json.dumps({'corrections':[],'extra':True}),cases)


def test_truncated_repetitive_response_is_not_shortened_in_capture(project):
    raw='{"corrections":['+('repeated JSON fragment '*1000)
    with pytest.raises(rev.ExperimentError,match='response_incomplete'):
        execute(project,client=client_for(output_text=raw,status='incomplete',incomplete_details=NS(reason='max_output_tokens')))
    result=rev.load_result(project/'experiments/revision','mini');capture=saved_response(project,result)
    assert capture['raw_generated_text']==raw
    assert capture['response_metadata']['incomplete_reason']=='max_output_tokens'
    assert any(e['category']=='invalid_json' for e in result['validation_errors'])


def test_invalid_unicode_preserved_as_utf8_json_evidence(project,cases):
    row=correction(cases);row['proposed_czech']='[Alex]\n<i>\ud800</i>'
    with pytest.raises(rev.ExperimentError,match='invalid_unicode'):execute(project,client=client_for([row]))
    result=rev.load_result(project/'experiments/revision','mini')
    assert result['rejected_proposals'][0]['proposal']==row
    assert saved_response(project,result)['raw_generated_text']
    for p in (project/'experiments/revision').rglob('*.json'):p.read_bytes().decode('utf-8')


def test_capture_excludes_credentials_headers_and_sdk_errors(project,cases,monkeypatch):
    monkeypatch.setenv('WP2_TEST_SECRET','environment-secret-value')
    row=correction(cases,'595','[smích]');row['explanation']='fake-offline-key Bearer bearer-secret-value api_key=sk-abcdefgh123456 environment-secret-value'
    client=client_for([correction(cases),row]);response=client.responses.create.return_value
    response.headers={'Authorization':'header-secret-value'}
    response.error=NS(message='sdk-secret-value')
    response.output=[NS(type='message',content=[NS(type='output_text',text=response.output_text)])]
    with pytest.raises(rev.ExperimentError,match='sensitive_content'):execute(project,client=client)
    result=rev.load_result(project/'experiments/revision','mini')
    assert [c['id'] for c in result['corrections']]==['1']
    assert result['rejected_proposals'][0]['diagnostic']['category']=='sensitive_content'
    evidence='\n'.join(p.read_text() for p in (project/'experiments/revision').rglob('*.json'))
    for secret in ['fake-offline-key','bearer-secret-value','sk-abcdefgh123456','environment-secret-value','header-secret-value','sdk-secret-value']:
        assert secret not in evidence
    assert '[REDACTED]' in evidence
    assert saved_response(project,result)['output_text_parts']


def test_refusal_is_captured_without_serializing_sdk_objects(project):
    response=NS(type='message',content=[NS(type='refusal',refusal='Cannot revise this input.')])
    with pytest.raises(rev.ExperimentError,match='response_incomplete'):
        execute(project,client=client_for(output=[response],output_text=''))
    result=rev.load_result(project/'experiments/revision','mini')
    assert saved_response(project,result)['output_text_parts']==[{'type':'refusal','text':'Cannot revise this input.'}]


def test_capture_failure_blocks_acceptance_but_charges_returned_usage(project,cases,monkeypatch):
    def fail(*_):raise OSError('secret filesystem error text')
    monkeypatch.setattr(rev,'capture_response',fail)
    with pytest.raises(rev.ExperimentError,match='response_capture_failure'):
        execute(project,client=client_for([correction(cases)]))
    result=rev.load_result(project/'experiments/revision','mini')
    assert result['status']=='failed' and result['response_capture'] is None
    assert tr.totals(ledger(project))[0]==Decimal('.0006015')
    assert 'secret filesystem error text' not in json.dumps(result)


@pytest.mark.parametrize('failure',['timeout','missing_usage','invalid_usage'])
def test_evidence_preserved_when_request_or_usage_fails(project,failure):
    client=client_for(output_text='{"corrections":[]}')
    if failure=='timeout':client.responses.create.side_effect=TimeoutError('credential in exception')
    elif failure=='missing_usage':client.responses.create.return_value.usage=None
    else:client.responses.create.return_value.usage.input_tokens=-1
    with pytest.raises(rev.ExperimentError):execute(project,client=client)
    result=rev.load_result(project/'experiments/revision','mini');capture=saved_response(project,result)
    assert capture['raw_generated_text']==(None if failure=='timeout' else '{"corrections":[]}')
    assert tr.totals(ledger(project))[1]>0
    assert 'credential in exception' not in json.dumps(result)+json.dumps(capture)


def seed_original_experiment(project):
    """Synthetic historical record, independent of future live experiment spending."""
    folder=project/'experiments/revision';folder.mkdir(parents=True,exist_ok=True)
    plan=rev.build_plan(project,'mini');state=rev.initial_ledger(plan['benchmark'])
    state['protocol_hash']=plan['protocol_hash']
    state['requests']=[{'id':'original-mini-attempt','experiment':'mini','model':'gpt-5.4-mini',
        'status':'settled','reservation':plan['estimate']['conservative_cost_usd'],
        'estimate':plan['estimate'],'protocol_hash':plan['protocol_hash'],
        'rates':{k:plan['price'][k] for k in ('input_per_million','cached_input_per_million','output_per_million')},
        'input_tokens':15298,'output_tokens':2428,'cached_tokens':0,'cost':'0.0223995',
        'validation':'unchanged_correction'}]
    rev.save_json(folder/'cost-ledger.json',state)
    rev.save_json(folder/'mini-results.json',{'status':'failed','corrections':[],
        'failure_category':'unchanged_correction','details':{'entry_id':'675'},'request_id':'original-mini-attempt'})
    return folder


def test_original_result_budget_and_prior_cost_preserved_on_fresh_run(project,cases):
    folder=seed_original_experiment(project);original=(folder/'mini-results.json').read_bytes();old=ledger(project)
    assert tr.totals(old)[0]==Decimal('.0223995') and old['budget_usd']=='1.00'
    result,_=execute(project,client=client_for([correction(cases),noop(cases)]))
    state=ledger(project)
    assert (folder/'mini-results.json').read_bytes()==original
    assert state['requests'][:-1]==old['requests'] and state['historical_v1']==old['historical_v1']
    assert state['budget_usd']=='1.00' and tr.totals(state)[0]==Decimal('.0230010')
    assert result['status']=='completed' and (folder/'mini-latest-results.json').exists()
    assert 'Domluv videohovor.' in rev.comparison(project)


def test_existing_ledger_and_original_result_unchanged_without_approval(project):
    folder=seed_original_experiment(project)
    before={p.name:p.read_bytes() for p in folder.glob('*.json')}
    factory=Mock(side_effect=AssertionError('No API client permitted'))
    rev.run_experiment(project,'mini',dry_run=True,client_factory=factory)
    rev.run_experiment(project,'mini',input_fn=lambda _:'n',client_factory=factory)
    assert {p.name:p.read_bytes() for p in folder.glob('*.json')}==before
    assert not (folder/'responses').exists()
    factory.assert_not_called()


def test_retry_does_not_overwrite_attempt_evidence(project,cases):
    with pytest.raises(rev.ExperimentError):execute(project,client=client_for(output_text='broken JSON'))
    first=rev.load_result(project/'experiments/revision','mini')
    folder=project/'experiments/revision'
    protected=[folder/first['response_capture'],folder/'attempts'/f"{first['request_id']}.json"]
    before={p:p.read_bytes() for p in protected}
    result,_=execute(project,client=client_for([correction(cases),noop(cases)]))
    assert result['request_id']!=first['request_id']
    assert {p:p.read_bytes() for p in protected}==before
    assert len(list((folder/'responses').glob('*.json')))==2
    assert len(ledger(project)['requests'])==2


def test_legacy_completed_result_still_prevents_spending(project):
    result,_=execute(project);folder=project/'experiments/revision'
    (folder/'mini-latest-results.json').rename(folder/'mini-results.json')
    factory=Mock()
    with pytest.raises(rev.ExperimentError,match='already completed'):
        rev.run_experiment(project,'mini',input_fn=lambda _:'y',client_factory=factory)
    factory.assert_not_called()
