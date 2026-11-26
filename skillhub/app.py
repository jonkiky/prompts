import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import Depends, FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import CLIENTS, MAX_FILE, MAX_ZIP, HubError, Store, digest, require, uid, unpack

DEMO_USERS = [
    ("author@example.test", "Jordan Lee", "employee"),
    ("reviewer@example.test", "Morgan Patel", "reviewer"),
    ("admin@example.test", "Sam Rivera", "admin"),
    ("employee@example.test", "Riley Kim", "employee"),
]


class CreateSkill(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1600)


class VersionCreate(BaseModel):
    label: str


class EditVersion(BaseModel):
    revision: int
    changes: dict = Field(default_factory=dict)


class EditFile(BaseModel):
    revision: int
    path: str
    content: str


class Action(BaseModel):
    action: str
    revision: int
    comment: str = ""
    reviewer_id: str | None = None
    target: str | None = None


class InstallRequest(BaseModel):
    skill_id: str
    client: str


class Decision(BaseModel):
    approve: bool
    auto_update: bool = False


def create_app(root=None, auth_mode=None, base_url=None):
    store = Store(root or os.getenv("HUB_DATA", "data"))
    mode = auth_mode or os.getenv("HUB_AUTH_MODE", "demo")
    require(mode in ("demo", "oidc"), "HUB_AUTH_MODE must be demo or oidc", 500)
    public_url = (base_url or os.getenv("HUB_BASE_URL", "http://127.0.0.1:8765")).rstrip("/")
    require(mode != "oidc" or public_url.startswith("https://"), "OIDC requires an HTTPS public URL", 500)
    if mode == "demo":
        for email, name, role in DEMO_USERS:
            store.add_user(email, name, role)
    app = FastAPI(title="Company Skill Hub", version="0.1.0", description="Internal management portal, reviewed releases and consent-gated connector API.")
    app.state.store, app.state.mode = store, mode

    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({"detail": exc.message}, exc.status)

    @app.middleware("http")
    async def security(request, call_next):
        if mode == "demo" and request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"detail": "Demo authentication is restricted to loopback hosts"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
        return response

    def identity(request: Request):
        bearer = request.headers.get("authorization", "")
        token = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("hub_session", "")
        user = store.authenticate(token) if token else None
        require(user, "Sign in to continue", 401)
        if user["kind"] == "session" and request.method not in ("GET", "HEAD", "OPTIONS"):
            require(secrets.compare_digest(user["csrf"], request.headers.get("x-csrf-token", "")), "Invalid CSRF token", 403)
            origin = request.headers.get("origin")
            require(not origin or origin == str(request.base_url).rstrip("/") or origin == public_url, "Invalid origin", 403)
        return user

    def portal(user=Depends(identity)):
        require(user["kind"] == "session", "Browser session required for management and approval", 403)
        return user

    def session_response(user, target="/"):
        token, _ = store.credential(user)
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("hub_session", token, httponly=True, samesite="lax", secure=mode == "oidc", max_age=28800)
        return resp

    @app.get("/health")
    def health():
        return {"status": "ok", "auth_mode": mode}

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"auth_mode": mode, "demo_users": store.users() if mode == "demo" else [], "clients": CLIENTS}

    @app.post("/auth/demo")
    async def demo_login(request: Request):
        require(mode == "demo", "Demo login is disabled", 404)
        origin = request.headers.get("origin")
        require(not origin or origin == str(request.base_url).rstrip("/"), "Invalid origin", 403)
        body = await request.json()
        user = next((u for u in store.users() if u["id"] == body.get("user_id")), None)
        require(user, "Unknown demonstration identity", 422)
        return session_response(user)

    @app.get("/auth/login")
    def oidc_start():
        require(mode == "oidc", "Use demo sign-in", 404)
        issuer, client = os.environ["HUB_OIDC_ISSUER"].rstrip("/"), os.environ["HUB_OIDC_CLIENT_ID"]
        require(issuer.startswith("https://"), "OIDC issuer must use HTTPS", 500)
        discovery = httpx.get(issuer + "/.well-known/openid-configuration", timeout=10)
        discovery.raise_for_status()
        config = discovery.json()
        require(config["issuer"] == issuer, "Issuer mismatch", 500)
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        with store.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS oidc_states (state TEXT PRIMARY KEY,nonce TEXT,verifier TEXT,expires REAL)")
            db.execute("DELETE FROM oidc_states WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO oidc_states VALUES (?,?,?,?)", (digest(state.encode()), nonce, verifier, time.time()+600))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"client_id": client, "redirect_uri": public_url+"/auth/callback", "response_type": "code", "scope": "openid profile email", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        resp = RedirectResponse(config["authorization_endpoint"] + "?" + urlencode(params))
        resp.set_cookie("hub_oidc", state, httponly=True, secure=True, samesite="lax", max_age=600)
        return resp

    @app.get("/auth/callback")
    def oidc_callback(request: Request, code: str, state: str):
        require(mode == "oidc" and secrets.compare_digest(state, request.cookies.get("hub_oidc", "")), "Invalid login state", 400)
        with store.tx() as db:
            r = db.execute("SELECT * FROM oidc_states WHERE state=? AND expires>?", (digest(state.encode()), time.time())).fetchone()
            require(r, "Login expired", 400)
            db.execute("DELETE FROM oidc_states WHERE state=?", (digest(state.encode()),))
        issuer, client = os.environ["HUB_OIDC_ISSUER"].rstrip("/"), os.environ["HUB_OIDC_CLIENT_ID"]
        try:
            config = httpx.get(issuer+"/.well-known/openid-configuration", timeout=10).json()
            require(config.get("issuer") == issuer and all(str(config.get(k, "")).startswith("https://") for k in ("token_endpoint", "jwks_uri")), "Invalid identity provider configuration", 400)
            response = httpx.post(config["token_endpoint"], data={"grant_type": "authorization_code", "code": code, "client_id": client, "client_secret": os.getenv("HUB_OIDC_CLIENT_SECRET", ""), "redirect_uri": public_url+"/auth/callback", "code_verifier": r["verifier"]}, timeout=10)
            response.raise_for_status()
            token = response.json()["id_token"]
            header = jwt.get_unverified_header(token)
            require(header.get("alg") in ("RS256", "ES256"), "Unsupported ID token algorithm", 400)
            keys = httpx.get(config["jwks_uri"], timeout=10).json()["keys"]
            key = next(k for k in keys if k.get("kid") == header.get("kid"))
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[header["alg"]], audience=client, issuer=issuer, options={"require": ["exp", "iat", "sub", "nonce"]})
            require(claims["nonce"] == r["nonce"] and claims.get("email_verified") is True, "Verified identity and matching nonce required", 400)
            email = claims["email"].lower()
            allowed_domain = os.getenv("HUB_EMAIL_DOMAIN", "").lower()
            require(not allowed_domain or email.endswith("@"+allowed_domain), "This organization is not allowed", 403)
            admins = os.getenv("HUB_ADMIN_EMAILS", "").lower().split(",")
            reviewers = os.getenv("HUB_REVIEWER_EMAILS", "").lower().split(",")
            role = "admin" if email in admins else "reviewer" if email in reviewers else "employee"
            # Bind accounts to issuer + subject; email changes must not merge identities.
            with store.tx() as db:
                db.execute("CREATE TABLE IF NOT EXISTS oidc_accounts (subject TEXT PRIMARY KEY,user_id TEXT)")
                account = db.execute("SELECT user_id FROM oidc_accounts WHERE subject=?", (issuer+"|"+claims["sub"],)).fetchone()
                if account:
                    user = dict(db.execute("SELECT * FROM users WHERE id=?", (account[0],)).fetchone())
                else:
                    require(not db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(), "Identity link requires administrator review", 403)
                    user = {"id": uid(), "email": email, "name": claims.get("name", email), "role": role}
                    db.execute("INSERT INTO users VALUES (?,?,?,?)", tuple(user.values()))
                    db.execute("INSERT INTO oidc_accounts VALUES (?,?)", (issuer+"|"+claims["sub"], user["id"]))
            resp = session_response(user)
            resp.delete_cookie("hub_oidc")
            return resp
        except HubError:
            raise
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, StopIteration, ValueError) as e:
            raise HubError(400, "Company login could not be verified. Retry or contact your administrator.") from e

    @app.get("/api/me")
    def me(user=Depends(identity)):
        return {k: user[k] for k in ("id", "name", "email", "role", "csrf", "kind")}

    @app.post("/auth/logout")
    def logout(request: Request, user=Depends(portal)):
        with store.tx() as db:
            db.execute("DELETE FROM credentials WHERE hash=?", (digest(request.cookies["hub_session"].encode()),))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("hub_session")
        return resp

    @app.get("/api/users")
    def users(user=Depends(portal)):
        return store.users() if user["role"] == "admin" else [u for u in store.users() if u["role"] in ("reviewer", "admin")]

    @app.patch("/api/users/{ident}")
    async def user_edit(ident: str, request: Request, user=Depends(portal)):
        require(user["role"] == "admin", "Administrator required", 403)
        body = await request.json()
        require(body.get("role") in ("employee", "reviewer", "admin"), "Invalid role", 422)
        require(ident != user["id"], "Ask another administrator to change your role", 422)
        with store.tx() as db:
            require(db.execute("SELECT 1 FROM users WHERE id=?", (ident,)).fetchone(), "User not found", 404)
            db.execute("UPDATE users SET role=? WHERE id=?", (body["role"], ident))
            store.event(db, user, "role_changed", None, detail=ident+":"+body["role"])
        return {"ok": True}

    @app.get("/api/skills")
    def skills(deleted: bool = False, user=Depends(portal)):
        return store.listing(user, deleted)

    @app.post("/api/skills", status_code=201)
    def create(body: CreateSkill, user=Depends(portal)):
        return store.create(user, body.name, body.description)

    @app.post("/api/skills/import", status_code=201)
    async def import_skill(file: UploadFile = File(...), user=Depends(portal)):
        data = await file.read(MAX_ZIP+1)
        files = unpack(data)
        from .core import parse_skill
        parsed = parse_skill(files)
        return store.create(user, parsed["name"], parsed["description"], files)

    @app.get("/api/skills/{sid}")
    def detail(sid: str, user=Depends(portal)):
        return store.detail(sid, user)

    @app.post("/api/skills/{sid}/versions", status_code=201)
    def new_version(sid: str, body: VersionCreate, user=Depends(portal)):
        return store.new_version(sid, user, body.label)

    @app.post("/api/skills/{sid}/actions")
    def skill_action(sid: str, body: Action, user=Depends(portal)):
        return store.skill_action(sid, user, body.action, body.revision, body.target, body.comment)

    @app.patch("/api/versions/{vid}")
    def edit_version(vid: str, body: EditVersion, user=Depends(portal)):
        return store.edit(vid, user, body.revision, changes=body.changes)

    @app.post("/api/versions/{vid}/actions")
    def version_action(vid: str, body: Action, user=Depends(portal)):
        return store.transition(vid, user, body.action, body.revision, body.comment, body.reviewer_id)

    @app.get("/api/versions/{vid}/diff")
    def diff(vid: str, user=Depends(portal)):
        return store.diff(vid, user)

    @app.get("/api/versions/{vid}/file")
    def read_file(vid: str, path: str, download: bool = False, user=Depends(portal)):
        with store.tx() as db:
            _, v = store.version_row(db, vid, user)
        require(path in v["files"], "File not found", 404)
        data = store.object_get(v["files"][path])
        content_type = "image/png" if path.lower().endswith(".png") else "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "text/plain; charset=utf-8" if path.lower().endswith((".md", ".json", ".yaml", ".yml", ".txt", ".py")) else "application/octet-stream"
        response = Response(data, media_type=content_type)
        if download or content_type == "application/octet-stream":
            from urllib.parse import quote
            response.headers["Content-Disposition"] = "attachment; filename*=UTF-8''"+quote(Path(path).name)
        return response

    @app.put("/api/versions/{vid}/file")
    def edit_file(vid: str, body: EditFile, user=Depends(portal)):
        return store.edit(vid, user, body.revision, path=body.path, data=body.content.encode())

    @app.post("/api/versions/{vid}/file")
    async def upload_file(vid: str, path: str = Form(...), revision: int = Form(...), file: UploadFile = File(...), user=Depends(portal)):
        return store.edit(vid, user, revision, path=path, data=await file.read(MAX_FILE+1))

    @app.delete("/api/versions/{vid}/file")
    def delete_file(vid: str, path: str, revision: int, user=Depends(portal)):
        return store.edit(vid, user, revision, path=path, delete=True)

    @app.get("/api/versions/{vid}/package")
    def version_package(vid: str, user=Depends(portal)):
        with store.tx() as db:
            _, v = store.version_row(db, vid, user)
        from .core import zip_bytes
        return Response(zip_bytes(store.materialize(v)), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="skill-'+v["label"]+'.zip"'})

    @app.get("/api/catalog")
    def catalog(user=Depends(identity)):
        return store.catalog(user)

    @app.get("/api/catalog/{sid}")
    def release(sid: str, user=Depends(identity)):
        return store.catalog(user, sid)

    @app.get("/api/tokens")
    def tokens(user=Depends(portal)):
        with store.tx() as db:
            return [dict(r) for r in db.execute("SELECT hash AS id,name,expires FROM credentials WHERE user_id=? AND kind='connector'", (user["id"],))]

    @app.post("/api/tokens")
    async def token_create(request: Request, user=Depends(portal)):
        body = await request.json()
        token, _ = store.credential(user, "connector", str(body.get("name", "Personal connector"))[:100])
        return {"token": token, "note": "Save this once in your connector token file. It cannot manage or approve skills."}

    @app.delete("/api/tokens/{ident}")
    def token_delete(ident: str, user=Depends(portal)):
        with store.tx() as db:
            db.execute("DELETE FROM credentials WHERE user_id=? AND hash=? AND kind='connector'", (user["id"], ident))
        return {"ok": True}

    @app.post("/api/install-intents")
    def create_intent(body: InstallRequest, user=Depends(identity)):
        intent = store.intent(user, body.skill_id, body.client)
        intent["approval_url"] = public_url + "/#install/" + intent["id"]
        return intent

    @app.get("/api/install-intents/{ident}")
    def read_intent(ident: str, user=Depends(identity)):
        with store.tx() as db:
            r = db.execute("SELECT * FROM intents WHERE id=? AND user_id=? AND expires>?", (ident, user["id"], time.time())).fetchone()
            require(r, "Installation request not found or expired", 404)
            r = dict(r)
        r["release"] = store.catalog(user, r["skill_id"])
        require(r["release"]["package_hash"] == r["hash"], "Release changed; prepare again")
        return r

    @app.post("/api/install-intents/{ident}/decision")
    def decide(ident: str, body: Decision, user=Depends(portal)):
        return store.decide_intent(user, ident, body.approve, body.auto_update)

    @app.get("/api/install-intents/{ident}/package")
    def install_package(ident: str, user=Depends(identity)):
        return Response(store.intent_package(user, ident), media_type="application/zip")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
