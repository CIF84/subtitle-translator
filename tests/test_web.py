"""Every API operation uses mocks; inherited no_network forbids outbound sockets."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from datetime import date
from decimal import Decimal
import json
import shutil
import threading

import pytest
from fastapi.testclient import TestClient
from web.app import create_app
from web.service import Jobs, WebError, MAX_UPLOAD
import web.service as svc
import translate_srt as tr

ROOT=Path(__file__).parents[1]
# Independently authored terrarium/mineral examples; no episode excerpt.
SRT=b'1\n00:03:41,125 --> 00:03:44,875\n<i>The terrarium needs a lamp.</i>\n\n2\n00:03:45,000 --> 00:03:49,250\nWe need to catalogue the mineral samples.\n\n'

class Deferred:
    def __init__(self):self.tasks=[]
    def submit(self,fn,*args):self.tasks.append((fn,args))
    def run(self):
        fn,args=self.tasks.pop(0);fn(*args)
    def shutdown(self,**kwargs):pass

@pytest.fixture
def webroot(tmp_path,monkeypatch):
    shutil.copyfile(ROOT/'pricing.json',tmp_path/'pricing.json')
    class FixedDate(date):
        @classmethod
        def today(cls):return cls(2026,10,12)
    monkeypatch.setattr(svc,'date',FixedDate)
    return tmp_path

@pytest.fixture
def webclient(webroot):
    factory=Mock(side_effect=AssertionError('No API clients before approval'))
    app=create_app(webroot,factory,Deferred())
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        token=client.get('/api/session').json()['csrf_token']
        client.headers['x-csrf-token']=token
        yield client,app.state.jobs,factory

def upload(client,name='Movie.en.srt',data=SRT):
    return client.post('/api/jobs',params={'filename':name},content=data)

def test_upload_and_persistence_no_api(webclient,webroot):
    c,j,f=webclient;r=upload(c);assert r.status_code==201;r=r.json()
    assert r['entries']==2 and r['config']['target']=='cs' and r['config']['max_cost']=='1.00'
    assert r['status']=='uploaded' and r['spent']=='0'
    assert len(list((webroot/'jobs').glob('*/job.json')))==1
    assert c.get('/api/jobs/'+r['id']).json()['filename']=='Movie.en.srt'
    f.assert_not_called()

@pytest.mark.parametrize('name',['Movie.txt','../../Movie.srt','C:\\Movie.srt','bad\n.srt','.env','.SRT'])
def test_invalid_filenames_rejected(webclient,name):
    c,_,f=webclient;assert upload(c,name).status_code==400;f.assert_not_called()

@pytest.mark.parametrize('data',[b'',b'not subtitles',b'1\nwrong time\nhello',b'\xff\xfe',b'1\n00:03:41,125 --> 00:03:44,875\n<i>broken'])
def test_bad_srt_rejected(webclient,data):
    c,_,f=webclient;assert upload(c,data=data).status_code==400;f.assert_not_called()

def test_upload_size_limit(webclient):
    c,_,f=webclient;assert upload(c,data=b'x'*(MAX_UPLOAD+1)).status_code==413;f.assert_not_called()

def test_stage_a_estimate_is_engine_preflight_and_read_only(webclient,webroot):
    c,j,f=webclient;job=upload(c).json();ident=job['id']
    before={p:p.read_bytes() for p in webroot.rglob('*') if p.is_file()}
    quote=c.post(f'/api/jobs/{ident}/estimate/translation').json()
    plan=j.translation_plan(j.job(ident));estimates=plan[-1]
    assert Decimal(quote['estimated_cost'])==sum(e.expected_cost for e in estimates)
    assert Decimal(quote['conservative_cost'])==sum(e.upper_cost for e in estimates)*3
    assert quote['input_tokens']>0 and quote['output_tokens']>0 and len(quote['quote_id'])==64
    assert {p:p.read_bytes() for p in webroot.rglob('*') if p.is_file()}==before
    assert tr.Config().batch_size==25 and tr.Config().reasoning_effort=='none' and tr.Config().response_schema=='simple'
    f.assert_not_called()

def test_configure_models_languages_and_budget(webclient):
    c,_,f=webclient;ident=upload(c).json()['id']
    r=c.put(f'/api/jobs/{ident}/config',json={'source':'en','target':'de','max_cost':'2.00'})
    assert r.status_code==200 and r.json()['config']['target']=='de'
    for bad in ({'target':'../secret'},{'max_cost':'NaN'},{'translation_model':'unknown'},{'revision_enabled':'yes'},{'api_key':'should never be accepted'}):
        assert c.put(f'/api/jobs/{ident}/config',json=bad).status_code in (400,422)
    f.assert_not_called()

def test_host_origin_csrf_and_filesystem_boundaries(webclient):
    c,_,f=webclient
    assert c.get('/api/session',headers={'host':'evil.example'}).status_code==400
    assert c.get('/api/session',headers={'origin':'https://evil.example'}).status_code==403
    assert c.post('/api/jobs',params={'filename':'a.srt'},content=SRT,headers={'x-csrf-token':'wrong'}).status_code==403
    assert c.get('/api/session',headers={'sec-fetch-site':'cross-site'}).status_code==403
    assert c.get('/api/jobs/not-a-uuid').status_code==404
    for path in ('/.env','/static/../service.py','/input/Movie.en.srt','/jobs','/api/jobs/..%2Fsecret'):
        assert c.get(path).status_code in (404,400)
    assert c.get('/').headers['content-security-policy'].startswith("default-src 'self'")
    f.assert_not_called()

def test_source_tampering_fails_closed(webclient,webroot):
    c,j,f=webclient;ident=upload(c).json()['id']
    p=next((webroot/'input/web'/ident).glob('*.srt'));p.write_bytes(SRT+b'damaged')
    assert c.post(f'/api/jobs/{ident}/estimate/translation').status_code==400
    f.assert_not_called()

def test_refresh_lists_persistent_job(webclient,webroot):
    c,j,f=webclient;ident=upload(c).json()['id']
    new=Jobs(webroot,client_factory=f,executor=Deferred())
    assert new.public(ident)['entries']==2 and new.listing()[0]['id']==ident
    f.assert_not_called()


def translated_client(response_changes=None):
    response=NS(status='completed',incomplete_details=None,output=[],usage=NS(input_tokens=100,output_tokens=60,input_tokens_details=NS(cached_tokens=0),output_tokens_details=NS(reasoning_tokens=0)),
                output_text=json.dumps({'entries':[{'id':'1','text':'<i>Terárium potřebuje lampu.</i>'},{'id':'2','text':'Musím popsat vzorky minerálů.'}],'glossary':[]}))
    for k,v in (response_changes or {}).items():setattr(response,k,v)
    client=Mock(api_key='fake-test-api-key');client.responses.create.return_value=response
    return client


def paid_job(root,client=None):
    pool=Deferred();api=client or translated_client();factory=Mock(return_value=api)
    jobs=Jobs(root,factory,pool);ident=jobs.upload('Movie.en.srt',SRT)['id']
    quote=jobs.estimate(ident,'translation')
    return jobs,pool,api,factory,ident,quote


@pytest.mark.parametrize('approved',[False,None,'yes',1])
def test_spending_requires_literal_true(webroot,approved):
    j,p,a,f,i,q=paid_job(webroot)
    with pytest.raises(WebError,match='not approved'):j.start(i,'translation',approved,q['quote_id'])
    f.assert_not_called();assert not p.tasks
    assert not j.checkpoint(i).exists()


def test_translation_background_progress_and_duplicate_guard(webroot):
    j,p,a,f,i,q=paid_job(webroot)
    assert j.start(i,'translation',True,q['quote_id'])['status']=='translation_queued'
    f.assert_not_called()
    with pytest.raises(WebError,match='owns'):j.start(i,'translation',True,q['quote_id'])
    p.run();state=j.public(i)
    assert state['status']=='translated' and state['completed']==2 and state['has_translation']
    assert Decimal(state['translation_cost'])==Decimal('.000345') and state['revision_cost']=='0'
    a.responses.create.assert_called_once()
    assert j.download(i,'translated').exists()
    with pytest.raises(WebError,match='completed'):j.start(i,'translation',True,q['quote_id'])
    assert not p.tasks


def test_approval_quote_stale_after_budget_change(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.configure(i,{'max_cost':'2.00'})
    with pytest.raises(WebError,match='Estimate changed'):j.start(i,'translation',True,q['quote_id'])
    f.assert_not_called()


def test_budget_blocks_before_api(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.configure(i,{'max_cost':'.000001'});q=j.estimate(i,'translation')
    with pytest.raises(WebError,match='remaining'):j.start(i,'translation',True,q['quote_id'])
    f.assert_not_called()


def test_api_failure_preserves_checkpoint_usage_and_reapproval(webroot):
    api=translated_client({'status':'incomplete','incomplete_details':NS(reason='max_output_tokens')})
    j,p,a,f,i,q=paid_job(webroot,api);j.start(i,'translation',True,q['quote_id']);p.run()
    assert j.public(i)['status']=='failed' and j.public(i)['completed']==0
    assert Decimal(j.public(i)['translation_cost'])==Decimal('.000345')
    new=Jobs(webroot,lambda:translated_client(),Deferred())
    assert new.public(i)['spent']==j.public(i)['spent']
    newq=new.estimate(i,'translation')
    with pytest.raises(WebError,match='Estimate changed'):new.start(i,'translation',True,q['quote_id'])
    new.start(i,'translation',True,newq['quote_id']);new.pool.run()
    assert new.public(i)['status']=='translated' and Decimal(new.public(i)['translation_cost'])==Decimal('.000690')


def test_queued_pause_makes_no_api_calls(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id']);j.pause(i);p.run()
    assert j.public(i)['status']=='paused';f.assert_not_called()


def test_restart_never_automatically_runs_queued_job(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id'])
    new=Jobs(webroot,f,Deferred());assert new.public(i)['status']=='interrupted'
    p.run();f.assert_not_called()


def test_safe_background_error_never_exposes_credentials(webroot):
    j,p,a,f,i,q=paid_job(webroot);a.models.retrieve.side_effect=RuntimeError('Authorization secret-key')
    j.start(i,'translation',True,q['quote_id']);p.run()
    assert j.public(i)['status']=='failed'
    assert 'secret-key' not in json.dumps(j.public(i))


def test_timestamps_utf8_and_formatting_in_saved_output(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id']);p.run()
    output=j.download(i,'translated').read_text(encoding='utf-8');entries=tr.parse_srt(output)
    assert [(e.id,e.timing) for e in entries]==[(e.id,e.timing) for e in tr.parse_srt(SRT.decode())]
    assert entries[0].text=='<i>Terárium potřebuje lampu.</i>'


def test_collision_safe_outputs_and_original_subtitles_preserved(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id']);p.run()
    original=j.download(i,'translated');before=original.read_bytes()
    second=j.upload('Movie.en.srt',SRT)['id'];q=j.estimate(second,'translation');j.start(second,'translation',True,q['quote_id']);p.run()
    assert j.download(second,'translated')!=original and original.read_bytes()==before
    with pytest.raises(WebError):j.configure(i,{'target':'de'})


def test_worker_status_remains_responsive_during_api_request(webroot):
    started=threading.Event();finish=threading.Event();api=translated_client();response=api.responses.create.return_value
    def block(**kwargs):started.set();assert finish.wait(5);return response
    api.responses.create.side_effect=block
    j=Jobs(webroot,lambda:api);i=j.upload('Movie.srt',SRT)['id'];q=j.estimate(i,'translation')
    j.start(i,'translation',True,q['quote_id']);assert started.wait(5)
    state=j.public(i);assert state['status']=='translation_running' and Decimal(state['reservations'])>0
    j.pause(i);finish.set();j.close();assert j.public(i)['completed']==2


def revision_client(rows=None,**overrides):
    client=translated_client()
    response=client.responses.create.return_value
    response.output_text=json.dumps({'corrections':rows or []},ensure_ascii=False)
    response.usage=NS(input_tokens=200,output_tokens=120,input_tokens_details=NS(cached_tokens=0),output_tokens_details=NS(reasoning_tokens=0))
    for k,v in overrides.items():setattr(response,k,v)
    return client


def revision_row(ident='2',old='Musím popsat vzorky minerálů.',new='Musíme popsat vzorky minerálů.'):
    return dict(id=ident,existing_czech=old,proposed_czech=new,category='semantic_error',explanation='We is plural, not singular.',confidence='high')


def ready_revision(root, rows=None):
    j,p,a,f,i,q=paid_job(root);j.start(i,'translation',True,q['quote_id']);p.run()
    baseline=j.download(i,'translated').read_bytes()
    j.configure(i,{'revision_enabled':True})
    reviewer=revision_client([revision_row()] if rows is None else rows)
    j.client_factory=Mock(return_value=reviewer)
    quote=j.estimate(i,'revision')
    return j,p,reviewer,i,quote,baseline


def generate_revision(root,rows=None):
    j,p,a,i,q,b=ready_revision(root,rows);j.start(i,'revision',True,q['quote_id']);p.run()
    assert j.public(i)['revision_complete']
    return j,p,a,i,b


def test_revision_preview_never_approves_or_calls_api(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.configure(i,{'revision_enabled':True})
    q=j.estimate(i,'translation')
    assert q['revision_preview']['provisional'] and Decimal(q['revision_preview']['estimated_cost'])>0
    f.assert_not_called();assert not p.tasks
    with pytest.raises(WebError):j.estimate(i,'revision')


def test_revision_is_separately_approved_and_leaves_translation_unchanged(webroot):
    j,p,a,i,q,b=ready_revision(webroot)
    assert not j.public(i)['revision_complete'];j.client_factory.assert_not_called()
    with pytest.raises(WebError):j.start(i,'revision',False,q['quote_id'])
    j.client_factory.assert_not_called()
    j.start(i,'revision',True,q['quote_id']);p.run()
    state=j.public(i);assert state['status']=='review' and state['review']['remaining']==1
    assert state['translation_cost']=='0.000345' and state['revision_cost']=='0.0023'
    assert j.download(i,'translated').read_bytes()==b
    suggestion=j.suggestions(i)[0]
    assert suggestion['original']=='We need to catalogue the mineral samples.' and suggestion['current']=='Musím popsat vzorky minerálů.'
    a.responses.create.assert_called_once();a.models.retrieve.assert_called_once_with('gpt-5.4')
    with pytest.raises(WebError,match='explicit'):j.export(i)


@pytest.mark.parametrize('action,text,expected',[('accept',None,'Musíme popsat vzorky minerálů.'),('reject',None,'Musím popsat vzorky minerálů.'),('edit','Je třeba popsat vzorky minerálů.','Je třeba popsat vzorky minerálů.')])
def test_accept_reject_edit_export_and_persistence(webroot,action,text,expected):
    j,p,a,i,b=generate_revision(webroot)
    j.decide(i,'2',action,text)
    new=Jobs(webroot,executor=Deferred())
    assert new.suggestions(i)[0]['decision']['action']==action
    assert new.public(i)['review']['remaining']==0
    final=new.export(i);assert final['has_final']
    output=new.download(i,'revised').read_text();entries=tr.parse_srt(output)
    assert entries[1].text==expected and entries[0].text=='<i>Terárium potřebuje lampu.</i>'
    assert [(e.id,e.timing) for e in entries]==[(e.id,e.timing) for e in tr.parse_srt(SRT.decode())]
    assert new.download(i,'translated').read_bytes()==b
    before=new.download(i,'revised').read_bytes();assert new.export(i)['has_final']
    assert new.download(i,'revised').read_bytes()==before


@pytest.mark.parametrize('text',['','<i>broken','<b>Changed</b>','A\n\nB','[Luca] extra'])
def test_invalid_edit_not_saved(webroot,text):
    j,p,a,i,b=generate_revision(webroot)
    with pytest.raises(tr.TranslationError):j.decide(i,'2','edit',text)
    assert j.public(i)['review']['remaining']==1
    with pytest.raises(WebError):j.export(i)


def test_edited_export_new_version_does_not_overwrite_old(webroot):
    j,p,a,i,b=generate_revision(webroot);j.decide(i,'2','accept');j.export(i)
    old=j.download(i,'revised');before=old.read_bytes()
    j.decide(i,'2','reject');j.export(i)
    assert j.download(i,'revised')!=old and old.read_bytes()==before


def test_revision_noop_validated_and_recorded_without_suggestion(webroot):
    j,p,a,i,q,b=ready_revision(webroot,[revision_row('1','<i>Terárium potřebuje lampu.</i>','<i>Terárium potřebuje lampu.</i>')])
    j.start(i,'revision',True,q['quote_id']);p.run()
    assert j.public(i)['review']['total']==0 and j.public(i)['revision_complete']
    assert j.revision_state(j.job(i))['noops'][0]['id']=='1'
    assert j.export(i)['has_final']


@pytest.mark.parametrize('damage',['id','duplicate','baseline','formatting','incomplete','invalid_json'])
def test_invalid_revision_is_not_displayed_but_cost_and_evidence_preserved(webroot,damage):
    row=revision_row();rows=[row]
    if damage=='id':row['id']='999'
    elif damage=='duplicate':rows.append(dict(row))
    elif damage=='baseline':row['existing_czech']='wrong'
    elif damage=='formatting':row['proposed_czech']='<i>bad'
    j,p,a,i,q,b=ready_revision(webroot,rows)
    if damage=='incomplete':a.responses.create.return_value.status='incomplete'
    if damage=='invalid_json':a.responses.create.return_value.output_text='broken json'
    j.start(i,'revision',True,q['quote_id']);p.run()
    assert j.public(i)['status']=='failed' and Decimal(j.public(i)['revision_cost'])==Decimal('.0023')
    assert not j.public(i)['revision_complete'] and j.public(i)['review']['total']==0
    with pytest.raises(WebError):j.suggestions(i)
    assert list((j.folder(i)/'revision-responses').glob('*.json'))
    assert j.download(i,'translated').read_bytes()==b
    a.responses.create.assert_called_once()


def test_revision_budget_includes_translation_cost_before_dispatch(webroot):
    j,p,a,i,q,b=ready_revision(webroot)
    j.configure(i,{'max_cost':'.01'});q=j.estimate(i,'revision')
    with pytest.raises(WebError,match='budget'):j.start(i,'revision',True,q['quote_id'])
    j.client_factory.assert_not_called()
    assert j.public(i)['spent']=='0.000345'


def test_revision_usage_unknown_reservation_survives_restart(webroot):
    j,p,a,i,q,b=ready_revision(webroot);a.responses.create.side_effect=TimeoutError('secret credentials')
    j.start(i,'revision',True,q['quote_id']);p.run()
    state=j.public(i);assert state['status']=='failed' and Decimal(state['reservations'])>0
    assert 'secret credentials' not in json.dumps(state)
    new=Jobs(webroot,executor=Deferred());assert new.public(i)['reservations']==state['reservations']


def test_revision_resume_does_not_drop_failed_spending(webroot):
    j,p,a,i,q,b=ready_revision(webroot);a.responses.create.return_value.output_text='bad json'
    j.start(i,'revision',True,q['quote_id']);p.run()
    first=j.revision_state(j.job(i))['requests'][0].copy()
    j.client_factory=lambda:revision_client([revision_row()]);q=j.estimate(i,'revision')
    j.start(i,'revision',True,q['quote_id']);p.run()
    ledger=j.revision_state(j.job(i));assert ledger['requests'][0]==first and len(ledger['requests'])==2
    assert j.public(i)['revision_cost']=='0.0046' and j.public(i)['review']['remaining']==1


def test_revision_context_and_no_automatic_confidence_acceptance(webroot):
    j,p,a,i,b=generate_revision(webroot)
    assert len(j.context(i,'2'))==2
    assert j.public(i)['review']['remaining']==1 and j.job(i)['decisions']=={}
    with pytest.raises(WebError):j.decide(i,'999','accept')


def test_revision_credential_redaction_and_rejection(webroot):
    row=revision_row();row['explanation']='fake-test-api-key'
    j,p,a,i,q,b=ready_revision(webroot,[row]);j.start(i,'revision',True,q['quote_id']);p.run()
    assert j.public(i)['status']=='failed'
    for path in (j.folder(i)/'revision-responses').glob('*.json'):
        assert 'fake-test-api-key' not in path.read_text()


def test_revision_model_can_change_before_its_first_paid_run(webroot):
    j,p,a,i,q,b=ready_revision(webroot);j.configure(i,{'revision_model':'gpt-5.4-mini'})
    q=j.estimate(i,'revision');j.start(i,'revision',True,q['quote_id']);p.run()
    with pytest.raises(WebError):j.configure(i,{'revision_model':'gpt-5.4'})


def test_ledgers_cannot_silently_change_actual_cost(webroot):
    j,p,a,i,b=generate_revision(webroot)
    path=j.folder(i)/'revision-ledger.json';state=json.loads(path.read_text());state['requests'][0]['cost']='0'
    svc.save(path,state)
    with pytest.raises(WebError):j.public(i)


def test_api_approval_absent_or_coerced_is_rejected(webclient):
    c,j,f=webclient;i=upload(c).json()['id'];q=c.post(f'/api/jobs/{i}/estimate/translation').json()
    for body in ({'quote_id':q['quote_id']},{'approved':False,'quote_id':q['quote_id']},{'approved':'yes','quote_id':q['quote_id']},{'approved':1,'quote_id':q['quote_id']}):
        assert c.post(f'/api/jobs/{i}/approve/translation',json=body).status_code in (400,422)
    f.assert_not_called()


def test_utf8_bom_crlf_and_nonsequential_numbering(webclient,webroot):
    c,j,f=webclient;data=b'\xef\xbb\xbf'+SRT.replace(b'\n',b'\r\n').replace(b'1\r\n',b'003\r\n',1)
    r=upload(c,data=data);assert r.status_code==201
    i=r.json()['id'];assert j.source(j.job(i))[0]==data
    assert c.post(f'/api/jobs/{i}/estimate/translation').status_code==200
    f.assert_not_called()


def test_raw_chunked_upload_limits(webclient):
    c,j,f=webclient
    def chunks():
        yield b'a'*MAX_UPLOAD
        yield b'excess'
    r=c.post('/api/jobs?filename=a.srt',content=chunks())
    assert r.status_code==413;f.assert_not_called()


def test_source_output_and_checkpoint_symlinks_rejected(webroot):
    j,p,a,f,i,q=paid_job(webroot)
    source=j.root/'input/web'/i
    source.rename(j.root/'input/web/original')
    source.symlink_to(j.root/'input/web/original',target_is_directory=True)
    with pytest.raises(WebError):j.estimate(i,'translation')
    f.assert_not_called()


def test_artifact_path_traversal_and_symlink_boundary(webroot):
    j=Jobs(webroot,executor=Deferred())
    for name in ('../../.env','/etc/passwd','..\\secret.srt'):
        with pytest.raises(WebError):j.artifact_path('translated',name)
    (webroot/'output').mkdir();(webroot/'output/translated').symlink_to(webroot,target_is_directory=True)
    with pytest.raises(WebError):j.artifact_path('translated','safe.srt')


def test_missing_checkpoint_never_resets_spending(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id']);p.run()
    j.checkpoint(i).unlink()
    with pytest.raises(WebError,match='missing'):j.public(i)
    with pytest.raises(WebError):j.estimate(i,'revision')


def test_rerendered_translation_is_collision_safe_after_interruption(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.start(i,'translation',True,q['quote_id']);p.run()
    path=j.download(i,'translated');before=path.read_bytes();state=j.job(i)
    state.update(translation_file=None,status='interrupted');j.persist(state)
    q=j.estimate(i,'translation');assert q['batches']==0
    f.reset_mock();j.start(i,'translation',True,q['quote_id']);p.run()
    f.assert_not_called();assert path.read_bytes()==before and j.public(i)['has_translation']


def test_tampered_translation_not_revised_or_downloaded(webroot):
    j,p,a,i,q,b=ready_revision(webroot);path=j.download(i,'translated');path.write_bytes(b+b'damaged')
    with pytest.raises(tr.TranslationError):j.estimate(i,'revision')
    with pytest.raises(tr.TranslationError):j.download(i,'translated')
    j.client_factory.assert_not_called()


def test_source_other_language_revision_prompt_and_structure(webroot):
    j,p,a,f,i,q=paid_job(webroot);j.configure(i,{'source':'fr','target':'de','revision_enabled':True})
    q=j.estimate(i,'translation');assert 'revision_preview' in q
    job=j.job(i);plan=j.revision_plan(job,{e.id:e.text for e in j.source(job)[1]},prospective=True)
    request=plan['plans'][0]['request']
    assert 'French-to-German' in request['instructions'] and 'Existing German' in request['instructions']
    assert request['reasoning']=={'effort':'none'} and request['text']['format']['strict']
    f.assert_not_called()


def test_multiple_suggestions_require_all_explicit_decisions(webroot):
    rows=[revision_row('1','<i>Terárium potřebuje lampu.</i>','<i>Terárium potřebuje nové osvětlení.</i>'),revision_row()]
    j,p,a,i,b=generate_revision(webroot,rows)
    j.decide(i,'1','reject');assert j.public(i)['review']['remaining']==1
    with pytest.raises(WebError):j.export(i)
    j.decide(i,'2','accept');assert j.export(i)['has_final']


def test_api_suggestions_decisions_context_and_download_flow(webroot):
    j,p,a,i,b=generate_revision(webroot)
    app=create_app(webroot,executor=Deferred())
    with TestClient(app,base_url='http://127.0.0.1:8000') as c:
        c.headers['x-csrf-token']=c.get('/api/session').json()['csrf_token']
        assert c.get(f'/api/jobs/{i}/suggestions').json()[0]['id']=='2'
        assert len(c.get(f'/api/jobs/{i}/context/2').json())==2
        assert c.post(f'/api/jobs/{i}/export').status_code==409
        assert c.put(f'/api/jobs/{i}/decisions/2',json={'action':'edit','text':'Musíme popsat vzorky minerálů.'}).status_code==200
        assert c.post(f'/api/jobs/{i}/export').status_code==200
        response=c.get(f'/api/jobs/{i}/download/revised')
        assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
        assert c.get(f'/api/jobs/{i}/download/translated').content==b
        assert '/Users/' not in json.dumps(c.get(f'/api/jobs/{i}').json())


def test_static_rendering_uses_textcontent_and_no_external_scripts(webclient):
    c,j,f=webclient
    js=c.get('/static/app.js').text;html=c.get('/').text
    assert '.innerHTML' not in js and '.textContent' in js and 'replaceChildren' in js
    assert 'http' not in html.split('<head>')[1].split('</head>')[0]
    assert 'high-confidence' not in js and 'api_key' not in js
    f.assert_not_called()


def test_invalid_changed_and_noop_revision_kept_as_evidence(webroot):
    row=revision_row('1','<i>Terárium potřebuje lampu.</i>','<i>Terárium potřebuje lampu.</i>');bad=revision_row();bad['id']='999'
    j,p,a,i,q,b=ready_revision(webroot,[row,bad]);j.start(i,'revision',True,q['quote_id']);p.run()
    audits=[svc.read(p) for p in (j.folder(i)/'revision-responses').glob('*-audit.json')]
    assert len(audits[0]['unchanged_proposals'])==1 and audits[0]['rejected_proposals'][0]['proposal']['id']=='999'
    assert j.public(i)['review']['total']==0


def test_capture_write_failure_still_settles_api_usage(webroot,monkeypatch):
    j,p,a,i,q,b=ready_revision(webroot);original=svc.save
    def fail_capture(path,data):
        if path.parent.name=='revision-responses':raise OSError('private secret trace')
        original(path,data)
    monkeypatch.setattr(svc,'save',fail_capture)
    j.start(i,'revision',True,q['quote_id']);p.run()
    assert j.public(i)['revision_cost']=='0.0023' and not j.public(i)['revision_complete']
    assert 'secret trace' not in json.dumps(j.public(i))


def test_duplicate_background_task_does_not_change_live_worker(webroot):
    started=threading.Event();finish=threading.Event();api=translated_client();response=api.responses.create.return_value
    def block(**kw):started.set();assert finish.wait(5);return response
    api.responses.create.side_effect=block
    j=Jobs(webroot,lambda:api);i=j.upload('Movie.srt',SRT)['id'];q=j.estimate(i,'translation');j.start(i,'translation',True,q['quote_id'])
    assert started.wait(5);j.worker(i,'translation');assert j.public(i)['status']=='translation_running'
    finish.set();j.close();api.responses.create.assert_called_once()


def test_launcher_bind_and_pid_cleanup(webroot,monkeypatch):
    import web.launch as launch
    import uvicorn
    fake_socket=Mock();monkeypatch.setattr(launch.socket,'socket',lambda *a:fake_socket)
    monkeypatch.setattr(launch.threading,'Thread',Mock())
    run=Mock();monkeypatch.setattr(uvicorn,'run',run)
    assert launch.run('start',webroot)==0
    fake_socket.bind.assert_called_once_with(('127.0.0.1',8000))
    assert run.call_args.kwargs['host']=='127.0.0.1' and run.call_args.kwargs['workers']==1
    assert not (webroot/'jobs/server.pid.json').exists()


def test_launcher_duplicate_opens_existing_without_starting(webroot,monkeypatch):
    import fcntl
    import web.launch as launch
    (webroot/'jobs').mkdir();path=webroot/'jobs/server.lock'
    with path.open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(launch,'health',lambda:{'application':'subtitle-translator-wp3','instance':'owned'})
        opened=Mock();monkeypatch.setattr(launch.webbrowser,'open',opened)
        assert launch.run('start',webroot)==0;opened.assert_called_once_with(launch.URL)


@pytest.mark.parametrize('matching',[False,True])
def test_stop_launcher_verifies_instance_before_signal(webroot,monkeypatch,matching):
    import fcntl
    import signal
    import web.launch as launch
    (webroot/'jobs').mkdir();svc.save(webroot/'jobs/server.pid.json',{'pid':98765,'instance':'ours'})
    with (webroot/'jobs/server.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(launch,'health',lambda:{'application':'subtitle-translator-wp3','instance':'ours' if matching else 'other','pid':98765})
        kill=Mock();monkeypatch.setattr(launch.os,'kill',kill)
        assert launch.run('stop',webroot)==(0 if matching else 1)
        if matching:kill.assert_called_once_with(98765,signal.SIGTERM)
        else:kill.assert_not_called()


def test_command_files_executable_and_no_credentials():
    for name in ('Start Subtitle Translator.command','Stop Subtitle Translator.command'):
        path=ROOT/name;assert path.stat().st_mode & 0o111
        text=path.read_text();assert '.venv/bin/python' in text and 'api_key' not in text


def response_for(text,status='completed'):
    return NS(status=status,incomplete_details=None,output=[],output_text=text,
        usage=NS(input_tokens=100,output_tokens=100,input_tokens_details=NS(cached_tokens=0),output_tokens_details=NS(reasoning_tokens=0)))


def test_multibatch_revision_resume_reuses_completed_work_and_retains_cost(webroot):
    raw=''.join(f'{n}\n00:00:{n:02},000 --> 00:00:{n+1:02},000\nLine {n}.\n\n' for n in range(1,27)).encode()
    trans=Mock(api_key='fake-multi')
    def translate(**request):
        ids=request['text']['format']['schema']['properties']['entries']['items']['properties']['id']['enum']
        return response_for(json.dumps({'entries':[{'id':i,'text':'Překlad '+i+'.'} for i in ids],'glossary':[]}))
    trans.responses.create.side_effect=translate
    pool=Deferred();j=Jobs(webroot,lambda:trans,pool);ident=j.upload('Long.srt',raw)['id']
    q=j.estimate(ident,'translation');j.start(ident,'translation',True,q['quote_id']);pool.run()
    assert trans.responses.create.call_count==2 and j.public(ident)['completed']==26
    j.configure(ident,{'revision_enabled':True});revapi=Mock(api_key='fake-multi');calls=[]
    def revise(**request):
        cases=json.loads(request['input'])['review'];calls.append([c['id'] for c in cases])
        rows=[revision_row(c['id'],c['existing_czech'],'Lepší překlad '+c['id']+'.') for c in cases]
        return response_for(json.dumps({'corrections':rows}),status='incomplete' if len(calls)==2 else 'completed')
    revapi.responses.create.side_effect=revise;j.client_factory=lambda:revapi
    q=j.estimate(ident,'revision');j.start(ident,'revision',True,q['quote_id']);pool.run()
    assert j.public(ident)['status']=='failed' and len(j.revision_state(j.job(ident))['completed_ids'])==25
    first_records=j.revision_state(j.job(ident))['requests'].copy()
    new=Jobs(webroot,lambda:revapi,Deferred());q=new.estimate(ident,'revision');assert q['batches']==1
    new.start(ident,'revision',True,q['quote_id']);new.pool.run()
    assert calls[-1]==['26'] and len(new.suggestions(ident))==26
    assert new.revision_state(new.job(ident))['requests'][:2]==first_records
    assert new.public(ident)['revision_cost']=='0.00525'


def test_same_formatting_required_for_user_edit(webroot):
    row=revision_row('1','<i>Terárium potřebuje lampu.</i>','<i>Terárium potřebuje nové osvětlení.</i>')
    j,p,a,i,b=generate_revision(webroot,[row])
    for edit in ('Terárium potřebuje nové osvětlení.','<i>Terárium potřebuje nové osvětlení.','<b>Terárium potřebuje nové osvětlení.</b>'):
        with pytest.raises(tr.InvalidBatch):j.decide(i,'1','edit',edit)
    j.decide(i,'1','edit','<i>Terárium potřebuje\nlampu.</i>');j.export(i)
    assert '<i>Terárium potřebuje\nlampu.</i>' in j.download(i,'revised').read_text()


def test_completed_review_uses_billing_snapshot_after_prices_change(webroot):
    j,p,a,i,b=generate_revision(webroot)
    original_spent=j.public(i)['spent']
    pricing=svc.read(webroot/'pricing.json')
    for price in pricing['models'].values():
        price['input_per_million']=str(Decimal(price['input_per_million'])*2)
    svc.save(webroot/'pricing.json',pricing)
    new=Jobs(webroot,executor=Deferred())
    assert new.public(i)['spent']==original_spent
    assert new.suggestions(i)[0]['id']=='2'
    new.decide(i,'2','accept');new.export(i)
    assert new.download(i,'translated').read_bytes()==b
    assert new.public(i)['spent']==original_spent


def test_stale_prices_block_spending_but_not_completed_review(webroot,monkeypatch):
    j,p,a,i,b=generate_revision(webroot)
    class FutureDate(date):
        @classmethod
        def today(cls):return cls(2027,1,1)
    monkeypatch.setattr(svc,'date',FutureDate)
    assert j.suggestions(i)[0]['id']=='2'
    j.decide(i,'2','reject');j.export(i)
    assert j.download(i,'translated').read_bytes()==b
    pending=j.upload('Another.srt',SRT)['id']
    with pytest.raises(WebError,match='older than 30 days'):
        j.estimate(pending,'translation')


def test_queued_pricing_change_requires_new_approval_before_client_creation(webroot):
    j,p,a,f,i,q=paid_job(webroot)
    j.start(i,'translation',True,q['quote_id'])
    pricing=svc.read(webroot/'pricing.json');pricing['last_verified']='2026-10-12'
    svc.save(webroot/'pricing.json',pricing);p.run()
    f.assert_not_called()
    assert j.public(i)['status']=='failed' and j.public(i)['spent']=='0'
    assert 'changed after approval' in j.public(i)['error']


@pytest.mark.parametrize('damage',['baseline','formatting','timestamp','unexpected_field'])
def test_stored_suggestions_revalidated_before_review_and_export(webroot,damage):
    j,p,a,i,b=generate_revision(webroot)
    job=j.job(i);ledger=j.revision_state(job)
    for rows in (job['suggestions'],ledger['suggestions']):
        if damage=='baseline':rows[0]['existing_czech']='Not the original.'
        elif damage=='formatting':rows[0]['proposed_czech']='<i>broken'
        elif damage=='timestamp':rows[0]['timestamp']='00:00:00,000 --> 00:00:00,001'
        else:rows[0]['extra']='unexpected'
    j.persist(job);svc.save(j.folder(i)/'revision-ledger.json',ledger)
    with pytest.raises(WebError):j.suggestions(i)
    with pytest.raises(WebError):j.export(i)
    assert j.download(i,'translated').read_bytes()==b


def test_unknown_persisted_decision_cannot_replace_required_review(webroot):
    j,p,a,i,b=generate_revision(webroot);job=j.job(i)
    job['decisions']['999']={'action':'reject','text':'unchanged','at':0};j.persist(job)
    with pytest.raises(WebError,match='Unknown review decision'):
        j.export(i)


def test_long_output_names_retain_unique_job_identity(webroot):
    j=Jobs(webroot,executor=Deferred())
    name='Long filename '*20+'.en.srt'
    first=j.job(j.upload(name,SRT)['id']);second=j.job(j.upload(name,SRT)['id'])
    first_name=j.output_name(first);second_name=j.output_name(second)
    assert first_name!=second_name and first['id'] in first_name and second['id'] in second_name
    revised=svc.filename(Path(first_name).stem+'.123456abcdef.revised.srt')
    assert first['id'] in revised and revised.endswith('.123456abcdef.revised.srt')


def test_unicode_filename_byte_limits_preserve_collision_suffix(webroot):
    j=Jobs(webroot,executor=Deferred())
    job=j.job(j.upload('漢'*200+'.en.srt',SRT)['id'])
    name=j.output_name(job)
    assert len(job['filename'].encode('utf-8'))<=150
    assert job['id'] in name and len(name.encode('utf-8'))<=150
    revised=svc.filename(Path(name).stem+'.123456abcdef.revised.srt')
    assert job['id'] in revised and revised.endswith('.123456abcdef.revised.srt')


@pytest.mark.parametrize('assignment',['OPENAI_API_KEY=\n','OPENAI_API_KEY=""\n',"OPENAI_API_KEY='  '\n"])
def test_empty_local_key_fails_before_sdk_and_preserves_zero_spending(webroot,monkeypatch,assignment):
    import openai
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    (webroot/'.env').write_text(assignment)
    constructor=Mock(side_effect=openai.OpenAIError('Missing credentials.'))
    monkeypatch.setattr(openai,'OpenAI',constructor)
    pool=Deferred();j=Jobs(webroot,executor=pool)
    raw=(ROOT/'tests/fixtures/synthetic.en.srt').read_bytes()
    ident=j.upload('synthetic.en.srt',raw)['id'];quote=j.estimate(ident,'translation')
    j.start(ident,'translation',True,quote['quote_id']);pool.run()
    state=j.public(ident)
    assert state['status']=='failed' and state['completed']==0 and state['entries']==6
    assert 'API key is empty' in state['error'] and 'No API request was sent' in state['error']
    assert state['spent']=='0' and state['reservations']=='0' and state['remaining']=='1.00'
    assert svc.read(j.checkpoint(ident))['requests']==[]
    assert j.revision_state(j.job(ident))['requests']==[]
    assert (webroot/'.env').read_text()==assignment
    constructor.assert_not_called()
    recovered=Jobs(webroot,executor=Deferred())
    assert recovered.public(ident)['spent']=='0' and not recovered.pool.tasks


def test_default_client_environment_key_overrides_empty_dotenv(webroot,monkeypatch):
    import openai
    monkeypatch.setenv('OPENAI_API_KEY','fake-web-environment-key')
    (webroot/'.env').write_text('OPENAI_API_KEY=""\n')
    api=translated_client();constructor=Mock(return_value=api)
    monkeypatch.setattr(openai,'OpenAI',constructor)
    pool=Deferred();j=Jobs(webroot,executor=pool)
    ident=j.upload('Movie.en.srt',SRT)['id'];quote=j.estimate(ident,'translation')
    constructor.assert_not_called()
    j.start(ident,'translation',True,quote['quote_id']);pool.run()
    constructor.assert_called_once_with(api_key='fake-web-environment-key',base_url='https://api.openai.com/v1',max_retries=0,timeout=180.0)
    assert j.public(ident)['status']=='translated' and j.public(ident)['translation_cost']=='0.000345'
    api.responses.create.assert_called_once()
    assert 'fake-web-environment-key' not in json.dumps(j.public(ident))


def test_stop_launcher_rejects_pid_mismatch_even_with_matching_instance(webroot,monkeypatch):
    import fcntl
    import web.launch as launch
    (webroot/'jobs').mkdir();svc.save(webroot/'jobs/server.pid.json',{'pid':98765,'instance':'ours'})
    with (webroot/'jobs/server.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(launch,'health',lambda:{'application':'subtitle-translator-wp3','instance':'ours','pid':98766})
        kill=Mock();monkeypatch.setattr(launch.os,'kill',kill)
        assert launch.run('stop',webroot)==1;kill.assert_not_called()
