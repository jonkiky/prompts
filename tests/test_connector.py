import json
import tomllib

import pytest
from fastapi.testclient import TestClient

from skillhub.connector import Connector, config
from skillhub.core import HubError
from test_workflow import client, login, create, publish, token


def bridge(c, tmp, name='codex'):
    auth=token(c)
    reader=TestClient(c.app,base_url='http://localhost',headers=auth)
    connector=Connector('http://localhost',auth['Authorization'][7:],name,tmp)
    connector.http.close()
    connector.http=reader
    return connector


def test_cache_consent_integrity_updates_and_dependencies(client,tmp_path):
    c=client;login(c);s=publish(c,create(c));sid=s['id']
    b=bridge(c,tmp_path/'cache')
    pending=b.prepare_skill(sid)
    assert pending['status']=='pending' and not b.cache.exists()
    ident=pending['approval_url'].split('/')[-1]
    assert c.post('/api/install-intents/'+ident+'/decision',json={'approve':True,'auto_update':True}).status_code==200
    ready=b.prepare_skill(sid)
    assert ready['status']=='ready'
    from pathlib import Path
    skill=Path(ready['skill_file']);original=skill.read_text()
    skill.write_text('tampered cache')
    assert b.prepare_skill(sid)['status']=='ready' and skill.read_text()==original
    s=c.post('/api/skills/'+sid+'/versions',json={'label':'1.1.0'}).json();v=s['versions'][0]
    c.put('/api/versions/'+v['id']+'/file',json={'revision':v['revision'],'path':'note.txt','content':'new version'})
    s=publish(c,c.get('/api/skills/'+sid).json())
    updated=b.prepare_skill(sid)
    assert updated['status']=='ready' and updated['version']=='1.1.0'
    s=c.post('/api/skills/'+sid+'/versions',json={'label':'1.2.0'}).json();v=s['versions'][0]
    metadata={**v['meta'],'dependencies':['python:json']}
    c.patch('/api/versions/'+v['id'],json={'revision':v['revision'],'changes':{'meta':metadata}})
    s=publish(c,c.get('/api/skills/'+sid).json())
    assert b.prepare_skill(sid)['status']=='pending', 'Changed dependencies require new consent'
    assert len(b.get_status())==1


def test_decline_never_downloads(client,tmp_path):
    c=client;login(c);s=publish(c,create(c));b=bridge(c,tmp_path/'cache')
    p=b.prepare_skill(s['id']);ident=p['approval_url'].split('/')[-1]
    c.post('/api/install-intents/'+ident+'/decision',json={'approve':False})
    assert b.prepare_skill(s['id'])['status']=='denied' and not b.cache.exists()


def test_unavailable_hub_does_not_return_cache(tmp_path):
    b=Connector('http://127.0.0.1:1','not-a-real-token','codex',tmp_path)
    with pytest.raises(HubError):b.prepare_skill('unknown')


@pytest.mark.parametrize('name',['codex','cursor','copilot'])
def test_client_config(name,tmp_path):
    text=config(name,'http://localhost:8765',tmp_path/'token',tmp_path/'cache')
    parsed=tomllib.loads(text) if name=='codex' else json.loads(text)
    key={'codex':'mcp_servers','cursor':'mcpServers','copilot':'servers'}[name]
    entry=parsed[key]['company_skill_hub']
    assert entry['args']==['-m','skillhub.connector']
    assert entry['env']['HUB_CLIENT']==name
    assert 'HUB_TOKEN_FILE' in entry['env'] and 'HUB_TOKEN' not in entry['env']
