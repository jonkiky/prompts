"""Cryptographic/authentication contract tests with an isolated simulated provider.

These tests validate our callback logic, not a real company's IdP configuration.
"""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from skillhub.app import create_app


@pytest.mark.parametrize('failure',[None,'nonce','signature','email','audience','expired'])
def test_oidc_identity_verification(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('HUB_OIDC_ISSUER','https://idp.example.test')
    monkeypatch.setenv('HUB_OIDC_CLIENT_ID','test-client')
    monkeypatch.setenv('HUB_EMAIL_DOMAIN','example.company')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    public=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));public['kid']='test-key'
    metadata={'issuer':'https://idp.example.test','authorization_endpoint':'https://idp.example.test/authorize','token_endpoint':'https://idp.example.test/token','jwks_uri':'https://idp.example.test/keys'}
    def get(url,**kwargs):
        return httpx.Response(200,json={'keys':[public]} if url.endswith('/keys') else metadata,request=httpx.Request('GET',url))
    monkeypatch.setattr('skillhub.app.httpx.get',get)
    with TestClient(create_app(tmp_path,'oidc','https://skills.example.company'),base_url='https://skills.example.company',follow_redirects=False) as c:
        r=c.get('/auth/login');assert r.status_code==307
        params=parse_qs(urlparse(r.headers['location']).query)
        assert params['code_challenge_method']==['S256']
        claims={'iss':metadata['issuer'],'aud':'test-client','sub':'stable-company-subject','exp':int(time.time())+300,'iat':int(time.time()),'nonce':params['nonce'][0],'email':'employee@example.company','email_verified':True,'name':'Employee'}
        if failure=='nonce':claims['nonce']='wrong'
        if failure=='email':claims['email_verified']=False
        if failure=='audience':claims['aud']='different-client'
        if failure=='expired':claims['exp']=int(time.time())-60
        signing=rsa.generate_private_key(public_exponent=65537,key_size=2048) if failure=='signature' else key
        encoded=jwt.encode(claims,signing,algorithm='RS256',headers={'kid':'test-key'})
        def post(url,**kwargs):
            assert kwargs['data']['code_verifier']
            return httpx.Response(200,json={'id_token':encoded},request=httpx.Request('POST',url))
        monkeypatch.setattr('skillhub.app.httpx.post',post)
        callback=c.get('/auth/callback',params={'state':params['state'][0],'code':'example-authorization-code'})
        assert callback.status_code==(303 if failure is None else 400),callback.text
        assert c.get('/api/me').status_code==(200 if failure is None else 401)
        assert c.get('/auth/callback',params={'state':params['state'][0],'code':'replay'}).status_code==400
