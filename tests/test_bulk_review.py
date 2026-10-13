"""Offline bulk review/export regressions; source API calls are mocked."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
import uuid

from fastapi.testclient import TestClient
import pytest

import translate_srt as tr
from web import review
from web.app import create_app
from web.service import Jobs,WebError
from test_web import webroot,Deferred

EN=['<i>One.</i>','Two.','Three.','[Luca] Four.','- Five.\n- Stay.','[door creaks]\nSix.']
CS=['<i>Jedna.</i>','Dvě.','Tři.','[Luca] Čtyři.','- Pět.\n- Zůstaň.','[vrzání dveří]\nŠest.']
NEW=['<i>To je jedna.</i>','Jsou dvě.','Jsou tři.','[Luca] Jsou čtyři.','- Je jich pět.\n- Počkej.','[vrzání dveří]\nJe jich šest.']


def response(obj):
    return NS(status='completed',incomplete_details=None,output=[],output_text=json.dumps(obj,ensure_ascii=False),
              usage=NS(input_tokens=100,output_tokens=100,input_tokens_details=NS(cached_tokens=0),output_tokens_details=NS(reasoning_tokens=0)))


@pytest.fixture
def ready(webroot):
    pool=Deferred();translator=Mock(api_key='fake-review-key')
    def translate(**req):
        ids=req['text']['format']['schema']['properties']['entries']['items']['properties']['id']['enum']
        return response(dict(entries=[dict(id=i,text=CS[int(i)-1]) for i in ids],glossary=[]))
    translator.responses.create.side_effect=translate
    j=Jobs(webroot,lambda:translator,pool)
    raw=''.join(f'{i}\n00:00:{i:02},000 --> 00:00:{i+1:02},000\n{text}\n\n' for i,text in enumerate(EN,1)).encode()
    ident=j.upload('Review.en.srt',raw)['id'];q=j.estimate(ident,'translation');j.start(ident,'translation',True,q['quote_id']);pool.run()
    j.configure(ident,{'revision_enabled':True})
    api=Mock(api_key='fake-review-key')
    api.responses.create.return_value=response(dict(corrections=[dict(id=str(i),existing_czech=CS[i-1],proposed_czech=NEW[i-1],category='semantic_error',explanation='Source meaning.',confidence='medium') for i in range(1,7)]))
    j.client_factory=lambda:api;q=j.estimate(ident,'revision');j.start(ident,'revision',True,q['quote_id']);pool.run()
    assert j.public(ident)['review']['total']==6
    return j,ident,webroot


def operation(j,ident,fn=review.bulk,action='accept',token=None,op=None,confirmed=True):
    token=token or review.state(j,ident)['version'];op=op or uuid.uuid4().hex
    if fn==review.bulk:return fn(j,ident,action,token,op,confirmed)
    return fn(j,ident,token,op,confirmed)


def prior(j,i):
    j.decide(i,'1','accept');j.decide(i,'2','reject');j.decide(i,'3','edit','Moje úprava.')
    return deepcopy(j.job(i)['decisions'])


def protected(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*')
            if p.is_file() and p.name not in ('job.json','execution.lock') and not p.relative_to(root).parts[:2]==('output','revised')}


@pytest.mark.parametrize('action',['accept','reject'])
def test_bulk_only_undecided_preserves_all_existing_decisions_and_edit(ready,action):
    j,i,root=ready;old=prior(j,i);before=protected(root);operation(j,i,action=action)
    job=j.job(i)
    assert all(job['decisions'][k]==v for k,v in old.items())
    assert all(job['decisions'][str(k)]['action']==action for k in (4,5,6))
    assert job['review_bulk_snapshot']['decisions']==old and job['review_bulk_snapshot']['affected_ids']==['4','5','6']
    assert review.state(j,i)['counts']['undecided']==0 and review.state(j,i)['undo_available']
    assert protected(root)==before


@pytest.mark.parametrize('fn',[review.bulk,review.restore,review.export_current])
def test_explicit_confirmation_required_no_mutation(ready,fn):
    j,i,root=ready;before=j.folder(i).joinpath('job.json').read_bytes()
    with pytest.raises(WebError,match='confirmation'):operation(j,i,fn=fn,confirmed=False)
    assert j.folder(i).joinpath('job.json').read_bytes()==before


def test_zero_remaining_disables_backend_bulk(ready):
    j,i,_=ready;operation(j,i)
    before=j.folder(i).joinpath('job.json').read_bytes()
    with pytest.raises(WebError,match='No undecided'):operation(j,i)
    assert j.folder(i).joinpath('job.json').read_bytes()==before


def test_atomic_bulk_save_failure_leaves_original_decisions(ready,monkeypatch):
    j,i,_=ready;old=prior(j,i);before=j.folder(i).joinpath('job.json').read_bytes()
    monkeypatch.setattr(j,'persist',Mock(side_effect=OSError('disk failed')))
    with pytest.raises(OSError):operation(j,i)
    assert j.folder(i).joinpath('job.json').read_bytes()==before and j.job(i)['decisions']==old


def test_atomic_restore_failure_preserves_bulk_decisions(ready,monkeypatch):
    j,i,_=ready;prior(j,i);operation(j,i);before=j.folder(i).joinpath('job.json').read_bytes()
    monkeypatch.setattr(j,'persist',Mock(side_effect=OSError('disk failed')))
    with pytest.raises(OSError):operation(j,i,fn=review.restore)
    assert j.folder(i).joinpath('job.json').read_bytes()==before


def test_cross_instance_lock_prevents_concurrent_bulk_submission(ready,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    j,i,root=ready;other=Jobs(root,executor=Deferred());token=review.state(j,i)['version']
    entered=threading.Event();release=threading.Event();original=j.persist
    def paused_save(job):
        entered.set();assert release.wait(3);original(job)
    monkeypatch.setattr(j,'persist',paused_save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(operation,j,i,token=token)
        try:
            assert entered.wait(3)
            with pytest.raises(tr.TranslationError,match='already running'):operation(other,i,token=token)
        finally:release.set()
        first.result(timeout=3)
    assert review.state(j,i)['counts']['accepted']==6


def test_duplicate_and_stale_submissions_cannot_overwrite(ready):
    j,i,_=ready;token=review.state(j,i)['version'];op=uuid.uuid4().hex
    operation(j,i,token=token,op=op);before=j.folder(i).joinpath('job.json').read_bytes()
    with pytest.raises(WebError,match='changed'):operation(j,i,token=token,op=op)
    assert j.folder(i).joinpath('job.json').read_bytes()==before
    with pytest.raises(WebError,match='changed'):review.decision(j,i,'1','reject',expected=token)


def test_stale_after_individual_decision_is_rejected(ready):
    j,i,_=ready;token=review.state(j,i)['version']
    review.decision(j,i,'1','edit','<i>Moje jedna.</i>',expected=token)
    with pytest.raises(WebError,match='changed'):operation(j,i,token=token)
    assert len(j.job(i)['decisions'])==1


def test_persist_after_restart_and_switch_between_jobs(ready):
    j,i,root=ready;old=prior(j,i);operation(j,i,action='reject')
    after=deepcopy(j.job(i)['decisions']);new=Jobs(root,executor=Deferred())
    second=new.upload('Other.en.srt',b'1\n00:00:01,000 --> 00:00:02,000\nAnother.\n')['id']
    new.job(second)
    assert new.job(i)['decisions']==after and all(after[k]==v for k,v in old.items())
    assert review.state(new,i)['undo_available']


def test_undo_restores_pre_bulk_snapshot_and_prevents_aba_replay(ready):
    j,i,root=ready;old=prior(j,i);old_token=review.state(j,i)['version'];operation(j,i)
    new=Jobs(root,executor=Deferred());operation(new,i,fn=review.restore)
    assert new.job(i)['decisions']==old and review.state(new,i)['counts']['undecided']==3
    assert not review.state(new,i)['undo_available'] and review.state(new,i)['version']!=old_token
    with pytest.raises(WebError):operation(new,i,fn=review.restore)
    with pytest.raises(WebError,match='changed'):operation(new,i,token=old_token)


def test_individual_decisions_remain_editable_and_block_unsafe_undo(ready):
    j,i,_=ready;prior(j,i);operation(j,i);snapshot=deepcopy(j.job(i)['review_bulk_snapshot'])
    review.decision(j,i,'4','edit','[Luca] Moje čtyři.',expected=review.state(j,i)['version'])
    assert j.job(i)['decisions']['4']['text']=='[Luca] Moje čtyři.'
    assert not review.state(j,i)['undo_available'] and j.job(i)['review_bulk_snapshot']==snapshot
    with pytest.raises(WebError,match='newer decisions'):operation(j,i,fn=review.restore)


def test_partial_export_overlay_counts_utf8_timestamps_format_and_state_unchanged(ready):
    j,i,root=ready;old=prior(j,i);before=j.job(i);guard=protected(root);token=review.state(j,i)['version']
    result=operation(j,i,fn=review.export_current)
    path=review.current_path(j,i);raw=path.read_bytes();entries=tr.parse_srt(raw.decode('utf-8'))
    assert [e.text for e in entries]==[NEW[0],CS[1],'Moje úprava.',*CS[3:]]
    assert [(e.id,e.timing) for e in entries]==[(e.id,e.timing) for e in j.source(j.job(i))[1]]
    assert result['counts']==dict(total=6,accepted=1,rejected=1,edited=1,undecided=3)
    assert j.job(i)['decisions']==old and j.job(i)['status']==before['status'] and review.state(j,i)['version']==token
    assert j.download(i,'translated').read_text()==tr.render_output(j.source(j.job(i))[1],{str(n):s for n,s in enumerate(CS,1)})
    assert protected(root)==guard and path.name.endswith('.current.srt')
    with pytest.raises(WebError,match='explicit'):review.export_full(j,i)


def test_partial_export_without_decisions_retains_original_and_review_state(ready):
    j,i,_=ready;before=j.job(i);token=review.state(j,i)['version'];operation(j,i,fn=review.export_current)
    assert review.current_path(j,i).read_bytes()==j.download(i,'translated').read_bytes()
    assert j.job(i)['decisions']=={} and j.job(i)['status']==before['status']
    assert review.state(j,i)['counts']['undecided']==6 and review.state(j,i)['version']==token


def test_partial_export_persist_failure_keeps_decisions_and_only_complete_files(ready,monkeypatch):
    j,i,root=ready;prior(j,i);before=j.folder(i).joinpath('job.json').read_bytes()
    monkeypatch.setattr(j,'persist',Mock(side_effect=OSError('disk failed')))
    with pytest.raises(OSError):operation(j,i,fn=review.export_current)
    assert j.folder(i).joinpath('job.json').read_bytes()==before
    for path in (root/'output/revised').glob('*.srt'):
        assert len(tr.parse_srt(path.read_text()))==6


def test_partial_export_duplicate_is_idempotent_and_changed_export_collision_safe(ready):
    j,i,_=ready;prior(j,i);token=review.state(j,i)['version'];op=uuid.uuid4().hex
    operation(j,i,fn=review.export_current,token=token,op=op);first=review.current_path(j,i);raw=first.read_bytes()
    operation(j,i,fn=review.export_current,token=token,op=op)
    assert review.current_path(j,i)==first
    review.decision(j,i,'4','accept',expected=token);operation(j,i,fn=review.export_current)
    assert review.current_path(j,i)!=first and first.read_bytes()==raw


def test_full_export_existing_semantics_compatible(ready):
    j,i,_=ready;operation(j,i);full=review.export_full(j,i,review.state(j,i)['version'])
    assert full['has_final'] and [e.text for e in tr.parse_srt(j.download(i,'revised').read_text())]==NEW
    first=j.download(i,'revised');review.export_full(j,i);assert j.download(i,'revised')==first
    token=review.state(j,i)['version'];review.decision(j,i,'1','reject',expected=token)
    review.export_full(j,i);assert j.download(i,'revised')!=first and first.exists()


@pytest.mark.parametrize('damage',['unknown','invalid_action','empty','missing_tag','speaker'])
def test_invalid_stored_decisions_cannot_bulk_or_export(ready,damage):
    j,i,_=ready;prior(j,i);job=j.job(i)
    if damage=='unknown':job['decisions']['999']=job['decisions']['3'].copy()
    elif damage=='invalid_action':job['decisions']['3']['action']='automatic'
    else:
        if damage=='empty':job['decisions']['3']['text']=''
        elif damage=='missing_tag':job['decisions']['1']={'action':'edit','text':'Missing italic','at':0}
        else:job['decisions']['4']={'action':'edit','text':'Missing Luca label.','at':0}
    j.persist(job)
    with pytest.raises(tr.TranslationError):operation(j,i,fn=review.export_current)
    assert not (j.root/'output/revised').exists()


def test_export_collision_refuses_overwrite(ready,monkeypatch):
    j,i,_=ready;fixed=uuid.uuid4();monkeypatch.setattr(review.uuid,'uuid4',lambda:fixed)
    operation(j,i,fn=review.export_current);path=review.current_path(j,i);raw=path.read_bytes()
    review.decision(j,i,'1','accept',expected=review.state(j,i)['version'])
    with pytest.raises(WebError,match='collision'):operation(j,i,fn=review.export_current)
    assert path.read_bytes()==raw


def test_current_download_refuses_modified_file(ready):
    j,i,_=ready;operation(j,i,fn=review.export_current);path=review.current_path(j,i);path.write_text('corrupted')
    with pytest.raises(WebError,match='changed'):review.current_path(j,i)


def test_http_confirmation_csrf_tokens_and_review_recovery(ready):
    j,i,root=ready;prior(j,i);factory=Mock();app=create_app(root,factory,Deferred())
    with TestClient(app,base_url='http://127.0.0.1:8000') as c:
        token=c.get('/api/session').json()['csrf_token'];v=c.get(f'/api/jobs/{i}/review-state').json()['version']
        payload=dict(action='accept',expected_version=v,operation_id=uuid.uuid4().hex,confirmed=True)
        assert c.post(f'/api/jobs/{i}/review/bulk',json=payload).status_code==403
        c.headers['x-csrf-token']=token
        assert c.post(f'/api/jobs/{i}/review/bulk',json={**payload,'confirmed':False}).status_code==400
        assert c.post(f'/api/jobs/{i}/review/bulk',json={**payload,'confirmed':'true'}).status_code==422
        assert c.post(f'/api/jobs/{i}/review/bulk',json=payload).status_code==200
        assert c.post(f'/api/jobs/{i}/review/bulk',json=payload).status_code==409
        assert c.put(f'/api/jobs/{i}/decisions/1',json={'action':'reject','expected_version':v}).status_code==409
        assert c.put(f'/api/jobs/{i}/decisions/1',json={'action':'reject'}).status_code==409
        fresh=c.get(f'/api/jobs/{i}/review-state').json();assert fresh['undo_available']
        undo=dict(expected_version=fresh['version'],operation_id=uuid.uuid4().hex,confirmed=True)
        assert c.post(f'/api/jobs/{i}/review/restore',json=undo).status_code==200
        export={**undo,'operation_id':uuid.uuid4().hex,'expected_version':c.get(f'/api/jobs/{i}/review-state').json()['version']}
        assert c.post(f'/api/jobs/{i}/export/current',json=export).status_code==200
        assert c.get(f'/api/jobs/{i}/download/current').status_code==200
        assert c.get(f'/api/jobs/{i}/review-state').json()['counts']['undecided']==3
    factory.assert_not_called()
