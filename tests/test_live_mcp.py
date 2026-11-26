"""Run against a local demo server with HUB_LIVE_URL set. Never contacts a company server."""
import asyncio
import json
import os
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack_result(r):
    assert not r.isError
    value=r.structuredContent or json.loads(r.content[0].text)
    return value.get('result',value) if isinstance(value,dict) else value


@pytest.mark.parametrize('client_name',['codex','cursor','copilot'])
def test_live_mcp_consent_and_install(client_name,tmp_path):
    url=os.getenv('HUB_LIVE_URL')
    if not url:pytest.skip('Set HUB_LIVE_URL to the local demo server to run stdio MCP integration')
    assert url.startswith('http://127.0.0.1:'), 'Live fixture is restricted to loopback'
    with httpx.Client(base_url=url,follow_redirects=True,trust_env=False) as browser:
        bootstrap=browser.get('/api/bootstrap').json()
        assert bootstrap['auth_mode']=='demo'
        user=next(u for u in bootstrap['demo_users'] if u['email']=='employee@example.test')
        browser.post('/auth/demo',json={'user_id':user['id']}).raise_for_status()
        browser.headers['X-CSRF-Token']=browser.get('/api/me').json()['csrf']
        response=browser.post('/api/tokens',json={'name':'Temporary MCP verification'})
        response.raise_for_status()
        token=response.json()['token']
        token_file=tmp_path/'token';token_file.write_text(token);token_file.chmod(0o600)
        async def run():
            env={**os.environ,'HUB_URL':url,'HUB_TOKEN_FILE':str(token_file),'HUB_CLIENT':client_name,'HUB_CACHE':str(tmp_path/'cache')}
            params=StdioServerParameters(command=sys.executable,args=['-m','skillhub.connector'],env=env)
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    tools=await session.list_tools()
                    assert {t.name for t in tools.tools}=={'list_skills','prepare_skill','get_status'}
                    releases=unpack_result(await session.call_tool('list_skills',{'query':'CCDI presentation'}))
                    release=next(r for r in releases if r['name']=='CCDI Slide Generation')
                    result=unpack_result(await session.call_tool('prepare_skill',{'skill_id':release['id']}))
                    assert result['status'] in ('pending','ready'),result
                    if result['status']=='pending':
                        ident=result['approval_url'].split('/')[-1]
                        assert not (tmp_path/'cache').exists()
                        decision=browser.post('/api/install-intents/'+ident+'/decision',json={'approve':True})
                        decision.raise_for_status()
                        result=unpack_result(await session.call_tool('prepare_skill',{'skill_id':release['id']}))
                    assert result['status']=='ready',result
                    from pathlib import Path
                    assert (Path(result['path'])/'assets/reference.pptx').is_file()
                    assert (Path(result['path'])/'scripts/build_deck.py').is_file()
                    assert unpack_result(await session.call_tool('get_status',{}))[0]['version']==release['version']
        try:
            asyncio.run(run())
        finally:
            from skillhub.core import digest
            browser.delete('/api/tokens/'+digest(token.encode()))
