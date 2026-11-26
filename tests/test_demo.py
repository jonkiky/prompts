import pytest
from fastapi.testclient import TestClient

from skillhub.app import create_app
from skillhub.core import HubError
from skillhub.demo import seed_demo
from test_workflow import login


def test_demo_samples_levels_and_repeat_setup(tmp_path):
    ids = seed_demo(tmp_path)
    assert len(ids) == 3
    with TestClient(create_app(tmp_path, "demo", "http://localhost"), base_url="http://localhost") as c:
        login(c)
        skills = c.get('/api/skills').json()
        assert len(skills) == 3
        personal = next(s for s in skills if s['visibility'] == 'private')
        assert personal['name'] == 'Personal Writing Notes (Demo)'
        assert personal['states'] == ['draft']
        published = [s for s in skills if s['visibility'] == 'company']
        assert len(published) == 2 and all(s['states'] == ['published'] for s in published)
        s = c.get('/api/skills/'+personal['id']).json()
        v = s['versions'][0]
        assert c.patch('/api/versions/'+v['id'], json={'revision':v['revision'], 'changes':{'changelog':'My edited preferences'}}).status_code == 200
        deleted = published[0]
        assert c.post('/api/skills/'+deleted['id']+'/actions', json={'action':'delete', 'revision':deleted['revision']}).status_code == 200
        assert seed_demo(tmp_path) == []
        assert c.get('/api/skills/'+s['id']).json()['versions'][0]['changelog'] == 'My edited preferences'
        assert c.get('/api/skills/'+deleted['id']).status_code == 404
        login(c, 'employee@example.test')
        assert c.get('/api/skills/'+personal['id']).status_code == 404
        catalog = c.get('/api/catalog').json()
        assert len(catalog) == 1 and catalog[0]['id'] != deleted['id']


def test_demo_samples_disabled_in_company_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_AUTH_MODE', 'oidc')
    monkeypatch.setenv('HUB_BASE_URL', 'https://hub.example.test')
    with pytest.raises(HubError) as error:
        seed_demo(tmp_path)
    assert error.value.status == 403
    with create_app(tmp_path, 'oidc', 'https://hub.example.test').state.store.tx() as db:
        assert db.execute('SELECT count(*) FROM skills').fetchone()[0] == 0
