import io
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from skillhub.app import create_app
from skillhub.core import unpack, HubError


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, "demo", "http://localhost")
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def login(c, email="author@example.test"):
    users = c.get('/api/bootstrap').json()['demo_users']
    u = next(u for u in users if u['email'] == email)
    assert c.post('/auth/demo', json={'user_id': u['id']}).status_code == 200
    me = c.get('/api/me').json()
    c.headers['X-CSRF-Token'] = me['csrf']
    return me


def create(c):
    r=c.post('/api/skills',json={'name':'Team briefing','description':'Create a company briefing.'})
    assert r.status_code==201, r.text
    return r.json()


def publish(c,s,visibility='company'):
    login(c)
    v=s['versions'][0]
    r=c.patch('/api/versions/'+v['id'],json={'revision':v['revision'],'changes':{'visibility':visibility}})
    assert r.status_code==200,r.text
    v=r.json()['versions'][0]
    reviewer=next(u for u in c.get('/api/users').json() if u['role']=='reviewer')
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'submit','revision':v['revision'],'reviewer_id':reviewer['id']})
    assert r.status_code==200,r.text
    v=r.json()['versions'][0]
    login(c,'reviewer@example.test')
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'approve','revision':v['revision'],'comment':'Reviewed all files and the requested audience.'})
    assert r.status_code==200,r.text
    v=r.json()['versions'][0]
    login(c)
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'publish','revision':v['revision']})
    assert r.status_code==200,r.text
    return r.json()


def token(c):
    r=c.post('/api/tokens',json={'name':'Integration test'})
    assert r.status_code==200,r.text
    return {'Authorization':'Bearer '+r.json()['token']}


def test_full_release_and_private_access(client):
    c=client;login(c);s=create(c);sid=s['id'];v=s['versions'][0]
    login(c,'employee@example.test')
    assert c.get('/api/skills/'+sid).status_code==404
    assert c.get('/api/versions/'+v['id']+'/file',params={'path':'SKILL.md'}).status_code==404
    assert c.get('/api/catalog').json()==[]
    s=publish(c,s)
    login(c,'employee@example.test')
    assert c.get('/api/catalog').json()[0]['name']=='Team briefing'
    login(c)
    n=c.post('/api/skills/'+sid+'/versions',json={'label':'1.1.0'}).json()
    draft=n['versions'][0]
    assert draft['state']=='draft'
    login(c,'employee@example.test')
    public=c.get('/api/skills/'+sid).json()
    assert len(public['versions'])==1 and public['audit']==[] and public['reviews']==[]
    assert c.get('/api/versions/'+draft['id']+'/file',params={'path':'SKILL.md'}).status_code==404
    assert c.get('/api/skills').json()[0]['states']==['published']


def test_stale_edits_and_immutable_release(client):
    c=client;login(c);s=create(c);v=s['versions'][0]
    edit={'revision':0,'changes':{'changelog':'Review me'}}
    assert c.patch('/api/versions/'+v['id'],json=edit).status_code==200
    assert c.patch('/api/versions/'+v['id'],json=edit).status_code==409
    s=c.get('/api/skills/'+s['id']).json();s=publish(c,s);v=s['versions'][0]
    assert c.put('/api/versions/'+v['id']+'/file',json={'revision':v['revision'],'path':'x.txt','content':'tamper'}).status_code==409


def test_review_reopen_invalidation(client):
    c=client;login(c);s=create(c);v=s['versions'][0]
    assert c.post('/api/versions/'+v['id']+'/actions',json={'action':'publish','revision':0}).status_code==409
    reviewer=next(u for u in c.get('/api/users').json() if u['role']=='reviewer')
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'submit','revision':0,'reviewer_id':reviewer['id']});v=r.json()['versions'][0]
    assert c.post('/api/versions/'+v['id']+'/actions',json={'action':'approve','revision':v['revision'],'comment':'self'}).status_code==403
    login(c,'reviewer@example.test')
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'request_changes','revision':v['revision'],'comment':'Clarify the expected inputs'});v=r.json()['versions'][0]
    assert v['state']=='draft'
    login(c)
    assert len(c.get('/api/versions/'+v['id']+'/diff').json())==1
    assert c.post('/api/versions/'+v['id']+'/actions',json={'action':'publish','revision':v['revision']}).status_code==409


def test_browser_consent_cannot_be_forged_by_connector(client):
    c=client;login(c);s=publish(c,create(c));auth=token(c)
    intent=c.post('/api/install-intents',headers=auth,json={'skill_id':s['id'],'client':'codex'}).json()
    assert intent['status']=='pending'
    assert c.get('/api/install-intents/'+intent['id']+'/package',headers=auth).status_code==403
    assert c.post('/api/install-intents/'+intent['id']+'/decision',headers=auth,json={'approve':True}).status_code==403
    assert c.post('/api/skills',headers=auth,json={'name':'bad','description':'bad'}).status_code==403
    assert c.post('/api/install-intents/'+intent['id']+'/decision',json={'approve':True,'auto_update':True}).status_code==200
    package=c.get('/api/install-intents/'+intent['id']+'/package',headers=auth)
    assert package.status_code==200 and 'SKILL.md' in unpack(package.content)
    # Visibility revocation is checked even for an approved intent.
    login(c,'employee@example.test');employee_token=token(c)
    other=c.post('/api/install-intents',headers=employee_token,json={'skill_id':s['id'],'client':'cursor'}).json()
    assert c.post('/api/install-intents/'+other['id']+'/decision',json={'approve':True}).status_code==200
    login(c)
    s=c.get('/api/skills/'+s['id']).json()
    assert c.post('/api/skills/'+s['id']+'/actions',json={'action':'make_private','revision':s['revision']}).status_code==200
    assert c.get('/api/install-intents/'+other['id']+'/package',headers=employee_token).status_code==404


def test_delete_restore_archive(client):
    c=client;login(c);s=publish(c,create(c));sid=s['id'];v=s['versions'][0]
    r=c.post('/api/versions/'+v['id']+'/actions',json={'action':'deprecate','revision':v['revision'],'comment':'Use new workflow'});v=r.json()['versions'][0]
    assert c.get('/api/catalog').json()[0]['state']=='deprecated'
    assert c.post('/api/versions/'+v['id']+'/actions',json={'action':'archive','revision':v['revision'],'comment':'Retired'}).status_code==200
    assert c.get('/api/catalog').json()==[]
    s=c.get('/api/skills/'+sid).json()
    assert c.post('/api/skills/'+sid+'/actions',json={'action':'delete','revision':s['revision']}).status_code==200
    assert c.get('/api/skills/'+sid).status_code==404
    login(c,'admin@example.test');s=c.get('/api/skills/'+sid).json()
    assert s['deleted_at']
    assert c.post('/api/skills/'+sid+'/actions',json={'action':'restore','revision':s['revision']}).status_code==200
    assert c.get('/api/skills/'+sid).json()['deleted_at'] is None


@pytest.mark.parametrize('name',['../escape','/absolute','x/../../escape','x\\escape'])
def test_zip_traversal(name):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr(name,'bad')
    with pytest.raises(HubError):unpack(b.getvalue())


def test_duplicate_and_symlink_zip():
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        info=zipfile.ZipInfo('SKILL.md');info.external_attr=(stat.S_IFLNK|0o777)<<16
        z.writestr(info,'/secret')
    with pytest.raises(HubError):unpack(b.getvalue())


def test_csrf_and_host_validation(client):
    c=client;login(c);c.headers.pop('X-CSRF-Token')
    assert c.post('/api/skills',json={'name':'test','description':'test'}).status_code==403
    assert c.get('/health',headers={'host':'attacker.example'}).status_code==403


def test_private_history_does_not_become_public(client):
    c=client;login(c);s=publish(c,create(c),'private');private_version=s['versions'][0]['id']
    s=c.post('/api/skills/'+s['id']+'/versions',json={'label':'1.1.0'}).json()
    s=publish(c,s,'company')
    login(c,'employee@example.test')
    assert c.get('/api/skills/'+s['id']).status_code==200
    assert c.get('/api/versions/'+private_version+'/file',params={'path':'SKILL.md'}).status_code==404
    assert len(c.get('/api/skills/'+s['id']).json()['versions'])==1


def test_archived_release_is_hidden_from_employee(client):
    c=client;login(c);s=publish(c,create(c));v=s['versions'][0]
    c.post('/api/versions/'+v['id']+'/actions',json={'action':'archive','revision':v['revision'],'comment':'Retired'})
    login(c,'employee@example.test')
    assert c.get('/api/skills/'+s['id']).status_code==404
    assert c.get('/api/skills').json()==[]


def test_ownership_transfer_revokes_old_owner_and_restarts_review(client):
    c=client;login(c);s=create(c);sid=s['id'];v=s['versions'][0]
    reviewer=next(u for u in c.get('/api/users').json() if u['role']=='reviewer')
    c.post('/api/versions/'+v['id']+'/actions',json={'action':'submit','revision':0,'reviewer_id':reviewer['id']})
    login(c,'admin@example.test')
    recipient=next(u for u in c.get('/api/users').json() if u['email']=='employee@example.test')
    s=c.get('/api/skills/'+sid).json()
    r=c.post('/api/skills/'+sid+'/actions',json={'action':'transfer_owner','revision':s['revision'],'target':recipient['id']})
    assert r.status_code==200,r.text
    login(c,'employee@example.test')
    transferred=c.get('/api/skills/'+sid).json()
    assert transferred['owner_id']==recipient['id'] and transferred['versions'][0]['state']=='draft'
    assert transferred['versions'][0]['reviewer_id'] is None
    login(c)
    assert c.get('/api/skills/'+sid).status_code==404


def test_demoted_reviewer_loses_unpublished_access(client):
    c=client;login(c);s=publish(c,create(c));sid=s['id']
    s=c.post('/api/skills/'+sid+'/versions',json={'label':'1.1.0'}).json();v=s['versions'][0]
    reviewer=next(u for u in c.get('/api/users').json() if u['role']=='reviewer')
    c.post('/api/versions/'+v['id']+'/actions',json={'action':'submit','revision':v['revision'],'reviewer_id':reviewer['id']})
    login(c,'reviewer@example.test')
    assert c.get('/api/versions/'+v['id']+'/file',params={'path':'SKILL.md'}).status_code==200
    login(c,'admin@example.test')
    assert c.patch('/api/users/'+reviewer['id'],json={'role':'employee'}).status_code==200
    login(c,'reviewer@example.test')
    assert c.get('/api/versions/'+v['id']+'/file',params={'path':'SKILL.md'}).status_code==404
    detail=c.get('/api/skills/'+sid).json()
    assert len(detail['versions'])==1 and detail['audit']==[] and detail['reviews']==[]
    assert c.get('/api/skills').json()[0]['states']==['published']


def test_real_ccdi_import(client):
    import os
    from pathlib import Path
    from skillhub.core import zip_bytes
    source=Path(os.getenv('CCDI_SKILL_SOURCE','data/ccdi-source'))
    if not source.exists():pytest.skip('Local company reference is not distributed with source code')
    files={str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    c=client;login(c)
    r=c.post('/api/skills/import',files={'file':('ccdi.zip',zip_bytes(files),'application/zip')})
    assert r.status_code==201,r.text
    s=publish(c,r.json());v=s['versions'][0]
    assert 'assets/reference.pptx' in v['files']
    assert c.get('/api/versions/'+v['id']+'/file',params={'path':'assets/reference.pptx'}).content==files['assets/reference.pptx']
