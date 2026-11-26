"""Transactional workflow and content-addressed storage, independent of HTTP."""
import difflib
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import yaml

CLIENTS = ("codex", "cursor", "copilot")
MAX_ZIP = 25 * 1024 * 1024
MAX_FILE = 12 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024


class HubError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def require(ok, message, status=409):
    if not ok:
        raise HubError(status, message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def uid():
    return str(uuid.uuid4())


def safe_path(path):
    require(isinstance(path, str) and 0 < len(path) < 300, "Invalid file path", 422)
    require(not any(ord(c) < 32 for c in path) and "\\" not in path, "Unsafe file path", 422)
    p = PurePosixPath(path)
    require(not p.is_absolute() and all(x not in ("..", ".git") for x in p.parts), "Unsafe file path", 422)
    require(str(p) == path and ":" not in path and path != ".", "Non-canonical file path", 422)
    return path


def unpack(data):
    require(len(data) <= MAX_ZIP, "ZIP exceeds 25 MB", 422)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = [i for i in z.infolist() if not i.is_dir()]
            require(0 < len(entries) <= 400, "ZIP must contain 1 to 400 files", 422)
            require(sum(i.file_size for i in entries) <= MAX_EXPANDED, "Expanded ZIP exceeds 100 MB", 422)
            files = {}
            for i in entries:
                safe_path(i.filename)
                require(not stat.S_ISLNK(i.external_attr >> 16), "Symlinks are not allowed", 422)
                require(i.file_size <= MAX_FILE, "A file exceeds 12 MB", 422)
                require(i.filename not in files, "Duplicate ZIP path", 422)
                files[i.filename] = z.read(i)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as e:
        raise HubError(422, "Invalid, encrypted or unsupported ZIP") from e
    if "SKILL.md" not in files:
        roots = [k[:-8] for k in files if k.endswith("/SKILL.md") and k.count("/") == 1]
        require(len(roots) == 1 and all(k.startswith(roots[0]) for k in files), "ZIP needs one root SKILL.md", 422)
        files = {safe_path(k[len(roots[0]):]): v for k, v in files.items()}
    parse_skill(files)
    return files


def parse_skill(files):
    require("SKILL.md" in files, "SKILL.md is required", 422)
    try:
        s = files["SKILL.md"].decode("utf-8")
        front = s.split("---", 2)
        require(s.startswith("---\n") and len(front) == 3, "SKILL.md needs YAML frontmatter", 422)
        meta = yaml.safe_load(front[1])
        require(isinstance(meta, dict), "Invalid skill metadata", 422)
        name, desc = meta.get("name", ""), meta.get("description", "")
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) and len(name) <= 64, "Invalid skill name", 422)
        require(isinstance(desc, str) and 0 < len(desc) <= 1600, "Description is required and must be under 1600 characters", 422)
        return {"name": name, "description": desc}
    except (UnicodeError, yaml.YAMLError) as e:
        raise HubError(422, "SKILL.md must contain UTF-8 and valid YAML") from e


def zip_bytes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(safe_path(path), (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    return out.getvalue()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True)
        self.db_path = self.root / "hub.sqlite3"
        with self.tx() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY,email TEXT UNIQUE,name TEXT,role TEXT);
            CREATE TABLE IF NOT EXISTS credentials (hash TEXT PRIMARY KEY,user_id TEXT,kind TEXT,csrf TEXT,expires REAL,name TEXT);
            CREATE TABLE IF NOT EXISTS skills (id TEXT PRIMARY KEY,owner_id TEXT,name TEXT,description TEXT,visibility TEXT DEFAULT 'private',current_id TEXT,deleted_at TEXT,review_due TEXT,revision INTEGER DEFAULT 0,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY,skill_id TEXT,label TEXT,state TEXT,revision INTEGER DEFAULT 0,visibility TEXT,reviewer_id TEXT,approved_revision INTEGER,approved_hash TEXT,files TEXT,meta TEXT,changelog TEXT,package_hash TEXT,created_at TEXT,published_at TEXT, UNIQUE(skill_id,label));
            CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY,version_id TEXT,actor_id TEXT,decision TEXT,comment TEXT,revision INTEGER,content_hash TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT,skill_id TEXT,version_id TEXT,actor_id TEXT,action TEXT,detail TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY,user_id TEXT,client TEXT,skill_id TEXT,version_id TEXT,hash TEXT,status TEXT,auto_update INTEGER DEFAULT 0,expires REAL);
            CREATE TABLE IF NOT EXISTS grants (user_id TEXT,client TEXT,skill_id TEXT,version_id TEXT,hash TEXT,fingerprint TEXT,auto_update INTEGER,PRIMARY KEY(user_id,client,skill_id));
            CREATE TABLE IF NOT EXISTS oidc_states (state TEXT PRIMARY KEY,nonce TEXT,verifier TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS oidc_accounts (subject TEXT PRIMARY KEY,user_id TEXT);
            ''')

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def object_put(self, data):
        key = digest(data)
        path = self.objects / key
        if not path.exists():
            tmp = self.objects / ("tmp-" + uid())
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return key

    def object_get(self, key):
        require(bool(re.fullmatch(r"[a-f0-9]{64}", key)), "Invalid object key", 422)
        data = (self.objects / key).read_bytes()
        require(digest(data) == key, "Stored object failed integrity verification", 500)
        return data

    def add_user(self, email, name, role="employee"):
        require(role in ("employee", "reviewer", "admin"), "Invalid role", 422)
        with self.tx() as db:
            db.execute("INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET name=excluded.name", (uid(), email.lower(), name, role))
            return dict(db.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone())

    def users(self):
        with self.tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM users ORDER BY name")]

    def credential(self, user, kind="session", name=""):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.tx() as db:
            db.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?)", (digest(token.encode()), user["id"], kind, csrf, time.time() + (86400 * 30 if kind == "connector" else 28800), name))
        return token, csrf

    def authenticate(self, token):
        with self.tx() as db:
            r = db.execute("SELECT users.*,credentials.kind,credentials.csrf FROM credentials JOIN users ON users.id=credentials.user_id WHERE hash=? AND expires>?", (digest(token.encode()), time.time())).fetchone()
            return dict(r) if r else None

    def event(self, db, user, action, skill_id, version_id=None, detail=""):
        db.execute("INSERT INTO audit(skill_id,version_id,actor_id,action,detail,created_at) VALUES (?,?,?,?,?,?)", (skill_id, version_id, user["id"], action, detail[:1000], now()))

    def skill_row(self, db, sid, user, deleted=False):
        r = db.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
        assigned = db.execute("SELECT 1 FROM versions WHERE skill_id=? AND reviewer_id=?", (sid, user["id"])).fetchone()
        released = r and r['current_id'] and db.execute("SELECT 1 FROM versions WHERE id=? AND state IN ('published','deprecated')", (r['current_id'],)).fetchone()
        visible = r and (r["owner_id"] == user["id"] or user["role"] == "admin" or (assigned and user["role"] == "reviewer") or (r["visibility"] == "company" and released))
        require(visible and (not r["deleted_at"] or (deleted and user["role"] == "admin")), "Skill not found", 404)
        return dict(r)

    def version_row(self, db, vid, user):
        r = db.execute("SELECT * FROM versions WHERE id=?", (vid,)).fetchone()
        require(r, "Version not found", 404)
        s = self.skill_row(db, r["skill_id"], user)
        privileged = user["id"] == s["owner_id"] or user["role"] == "admin" or (user['role'] == 'reviewer' and user['id'] == r['reviewer_id'])
        require(privileged or (r["state"] in ("published", "deprecated") and r['visibility'] == 'company' and s["visibility"] == "company"), "Version not found", 404)
        v = dict(r)
        v["files"] = json.loads(v["files"])
        v["meta"] = json.loads(v["meta"])
        return s, v

    def author(self, s, user):
        require(s["owner_id"] == user["id"] or user["role"] == "admin", "Only the owner or administrator can edit", 403)

    def check_rev(self, row, revision):
        require(row["revision"] == revision, "This item changed. Refresh before continuing.")

    def materialize(self, v):
        return {p: self.object_get(h) for p, h in v["files"].items()}

    def content_hash(self, v):
        return digest(json.dumps({k: v[k] for k in ("files", "meta", "label", "visibility", "changelog")}, sort_keys=True).encode())

    def validate(self, v):
        files = self.materialize(v)
        require(len(files) <= 400 and sum(len(b) for b in files.values()) <= MAX_EXPANDED, "Package exceeds expanded limits", 422)
        meta = parse_skill(files)
        for path, data in files.items():
            if path.endswith(".md"):
                try:
                    text = data.decode()
                except UnicodeError:
                    raise HubError(422, "Markdown files must be UTF-8")
                for target in re.findall(r"\]\(([^)\s]+)\)", text):
                    if re.match(r"[a-z]+:|#|/", target):
                        continue
                    from posixpath import normpath, join, dirname
                    resolved = normpath(join(dirname(path), target.split("#")[0]))
                    require(resolved in files, f"Missing reference: {path} links to {target}", 422)
        if "artifact-template.json" in files:
            try:
                template = json.loads(files["artifact-template.json"])
                for key in ("reference", "preview"):
                    require(not template.get(key) or template[key] in files, f"Missing template asset: {key}", 422)
            except (ValueError, TypeError) as e:
                raise HubError(422, "Invalid artifact-template.json") from e
        return meta

    def create(self, user, name, description, files=None, meta=None):
        if files is None:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
            files = {"SKILL.md": ("---\n" + yaml.safe_dump({"name": slug, "description": description}, sort_keys=False) + "---\n\n# " + name + "\n\nDescribe the workflow here.\n").encode()}
        parsed = parse_skill(files)
        sid, vid = uid(), uid()
        inventory = {safe_path(p): self.object_put(b) for p, b in files.items()}
        metadata = {"display_name": name or parsed["name"], "dependencies": [], "compatibility": {c: "unverified" for c in CLIENTS}, **(meta or {})}
        with self.tx() as db:
            db.execute("INSERT INTO skills(id,owner_id,name,description,updated_at) VALUES (?,?,?,?,?)", (sid, user["id"], metadata["display_name"], parsed["description"], now()))
            db.execute("INSERT INTO versions(id,skill_id,label,state,visibility,files,meta,changelog,created_at) VALUES (?,?,?,'draft','private',?,?,?,?)", (vid, sid, "1.0.0", json.dumps(inventory), json.dumps(metadata), "Initial version", now()))
            self.event(db, user, "created", sid, vid)
        return self.detail(sid, user)

    def listing(self, user, include_deleted=False):
        with self.tx() as db:
            rows = []
            for item in db.execute("SELECT id FROM skills ORDER BY updated_at DESC").fetchall():
                try:
                    s = self.skill_row(db, item["id"], user, include_deleted)
                except HubError:
                    continue
                s["owner_name"] = db.execute("SELECT name FROM users WHERE id=?", (s["owner_id"],)).fetchone()[0]
                privileged = s["owner_id"] == user["id"] or user["role"] == "admin" or (user['role'] == 'reviewer' and db.execute("SELECT 1 FROM versions WHERE skill_id=? AND reviewer_id=?", (s["id"],user["id"])).fetchone())
                s["states"] = [r[0] for r in db.execute("SELECT DISTINCT state FROM versions WHERE skill_id=?", (s["id"],))] if privileged else [db.execute("SELECT state FROM versions WHERE id=?", (s["current_id"],)).fetchone()[0]]
                s["current_label"] = (db.execute("SELECT label FROM versions WHERE id=?", (s["current_id"],)).fetchone() or [None])[0]
                rows.append(s)
            return rows

    def detail(self, sid, user):
        with self.tx() as db:
            s = self.skill_row(db, sid, user, deleted=True)
            s["versions"] = []
            if not s["deleted_at"]:
                for row in db.execute("SELECT id FROM versions WHERE skill_id=? ORDER BY created_at DESC,rowid DESC", (sid,)).fetchall():
                    try:
                        _, v = self.version_row(db, row[0], user)
                        s["versions"].append(v)
                    except HubError:
                        pass
            privileged = user["role"] == "admin" or user["id"] == s["owner_id"] or (user['role'] == 'reviewer' and any(v["reviewer_id"] == user["id"] for v in s["versions"]))
            s["audit"] = [dict(r) for r in db.execute("SELECT audit.*,users.name AS actor FROM audit JOIN users ON users.id=audit.actor_id WHERE skill_id=? ORDER BY audit.id DESC LIMIT 100", (sid,))] if privileged else []
            s["reviews"] = [dict(r) for r in db.execute("SELECT reviews.*,users.name AS actor FROM reviews JOIN users ON users.id=reviews.actor_id JOIN versions ON versions.id=reviews.version_id WHERE versions.skill_id=? ORDER BY reviews.created_at DESC", (sid,))] if privileged else []
            return s

    def new_version(self, sid, user, label):
        require(bool(re.fullmatch(r"\d+\.\d+\.\d+", label)), "Use a semantic version such as 1.1.0", 422)
        with self.tx() as db:
            s = self.skill_row(db, sid, user)
            self.author(s, user)
            require(not db.execute("SELECT 1 FROM versions WHERE skill_id=? AND label=?", (sid, label)).fetchone(), "Version label already exists")
            r = db.execute("SELECT * FROM versions WHERE skill_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
            vid = uid()
            db.execute("INSERT INTO versions(id,skill_id,label,state,visibility,files,meta,changelog,created_at) VALUES (?,?,?,'draft',?,?,?,'',?)", (vid, sid, label, s["visibility"], r["files"], r["meta"], now()))
            self.event(db, user, "draft_created", sid, vid, label)
        return self.detail(sid, user)

    def edit(self, vid, user, revision, changes=None, path=None, data=None, delete=False):
        with self.tx() as db:
            s, v = self.version_row(db, vid, user)
            self.author(s, user)
            self.check_rev(v, revision)
            require(v["state"] == "draft", "Only Draft versions can be edited")
            if path is not None:
                safe_path(path)
                if delete:
                    require(path != "SKILL.md", "SKILL.md cannot be deleted", 422)
                    require(path in v["files"], "File not found", 404)
                    del v["files"][path]
                else:
                    require(len(data) <= MAX_FILE, "File exceeds 12 MB", 422)
                    v["files"][path] = self.object_put(data)
                    if path == "SKILL.md":
                        parse_skill({"SKILL.md": data})
            if changes:
                for k in ("changelog", "visibility"):
                    if k in changes:
                        v[k] = changes[k]
                require(v["visibility"] in ("private", "company"), "Invalid visibility", 422)
                if "meta" in changes:
                    v["meta"] = changes["meta"]
                    require(isinstance(v["meta"], dict) and isinstance(v["meta"].get("dependencies", []), list) and isinstance(v["meta"].get("compatibility", {}), dict), "Invalid dependency metadata", 422)
                    require(all(isinstance(d, str) and re.fullmatch(r"(?:python|capability|command):[a-zA-Z0-9_.-]+", d) for d in v["meta"].get("dependencies", [])), "Dependencies must use python:, command: or capability: names", 422)
                    require(all(k in CLIENTS and value in ("ready", "unverified", "requires_setup", "requires_adapter", "unsupported") for k,value in v["meta"].get("compatibility", {}).items()), "Invalid client compatibility", 422)
                if "review_due" in changes:
                    due = changes["review_due"] or None
                    if due:
                        from datetime import date
                        try:
                            date.fromisoformat(due)
                        except ValueError:
                            raise HubError(422, "Invalid review date")
                    db.execute("UPDATE skills SET review_due=? WHERE id=?", (due, s["id"]))
            db.execute("UPDATE versions SET files=?,meta=?,visibility=?,changelog=?,revision=revision+1,approved_hash=NULL,approved_revision=NULL WHERE id=?", (json.dumps(v["files"]), json.dumps(v["meta"]), v["visibility"], v["changelog"], vid))
            db.execute("UPDATE skills SET revision=revision+1,updated_at=? WHERE id=?", (now(), s["id"]))
            self.event(db, user, "draft_edited", s["id"], vid, path or "metadata")
        return self.detail(s["id"], user)

    def transition(self, vid, user, action, revision, comment="", reviewer_id=None):
        require(action in ("submit", "approve", "request_changes", "comment", "publish", "reopen", "deprecate", "archive"), "Unknown action", 422)
        with self.tx() as db:
            s, v = self.version_row(db, vid, user)
            self.check_rev(v, revision)
            if action in ("submit", "publish", "reopen"):
                self.author(s, user)
            if action == "submit":
                require(v["state"] == "draft", "Only drafts can be submitted")
                parsed = self.validate(v)
                reviewer = db.execute("SELECT * FROM users WHERE id=? AND role IN ('reviewer','admin')", (reviewer_id,)).fetchone()
                require(reviewer and reviewer["id"] != s["owner_id"], "Choose a reviewer other than the author", 422)
                v["state"], v["reviewer_id"] = "in_review", reviewer_id
            elif action in ("approve", "request_changes"):
                require(user["role"] in ("reviewer", "admin") and user["id"] != s["owner_id"], "An independent reviewer is required", 403)
                require(user["role"] == "admin" or v["reviewer_id"] == user["id"], "You are not assigned to this review", 403)
                require(v["state"] == "in_review", "Version is not in review")
                require(bool(comment.strip()), "Include a review comment", 422)
                v["state"] = "approved" if action == "approve" else "draft"
                v["approved_hash"] = self.content_hash(v) if action == "approve" else None
                v["approved_revision"] = v["revision"] + 1 if action == "approve" else None
            elif action == "publish":
                require(v["state"] == "approved" and v["approved_revision"] == v["revision"] and v["approved_hash"] == self.content_hash(v), "Exact revision must be approved before publication")
                parsed = self.validate(v)
                package = zip_bytes(self.materialize(v))
                require(len(package) <= MAX_ZIP, "Published ZIP exceeds 25 MB; reduce the package size", 422)
                v["package_hash"] = self.object_put(package)
                v["state"] = "published"
                db.execute("UPDATE skills SET name=?,description=?,visibility=?,current_id=?,revision=revision+1,updated_at=? WHERE id=?", (v["meta"].get("display_name") or parsed["name"], parsed["description"], v["visibility"], vid, now(), s["id"]))
            elif action == "reopen":
                require(v["state"] in ("approved", "in_review"), "Create a new draft to edit a published version")
                v["state"], v["approved_hash"], v["approved_revision"] = "draft", None, None
            elif action in ("deprecate", "archive"):
                self.author(s, user)
                require(v["state"] in ("published", "deprecated"), "Only released versions can be retired")
                require(bool(comment.strip()), "Include a retirement reason or replacement", 422)
                v["state"] = "deprecated" if action == "deprecate" else "archived"
            if action in ("approve", "request_changes", "comment"):
                require(bool(comment.strip()), "A comment is required", 422)
                db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?)", (uid(), vid, user["id"], action, comment[:4000], v["revision"], self.content_hash(v), now()))
            if action != "comment":
                db.execute("UPDATE versions SET state=?,revision=revision+1,reviewer_id=?,approved_hash=?,approved_revision=?,package_hash=?,published_at=? WHERE id=?", (v["state"], v["reviewer_id"], v["approved_hash"], v["approved_revision"], v["package_hash"], now() if action == "publish" else v["published_at"], vid))
            self.event(db, user, action, s["id"], vid, comment)
        return self.detail(s["id"], user)

    def skill_action(self, sid, user, action, revision, target=None, reason=""):
        with self.tx() as db:
            s = self.skill_row(db, sid, user, deleted=True)
            self.check_rev(s, revision)
            self.author(s, user)
            if action == "delete":
                db.execute("UPDATE skills SET deleted_at=? WHERE id=?", (now(), sid))
            elif action == "restore":
                require(user["role"] == "admin", "Only administrators can restore", 403)
                db.execute("UPDATE skills SET deleted_at=NULL WHERE id=?", (sid,))
            elif action == "make_private":
                db.execute("UPDATE skills SET visibility='private' WHERE id=?", (sid,))
            elif action == "transfer_owner":
                require(user['role'] == 'admin', 'Only administrators can transfer ownership', 403)
                require(db.execute('SELECT 1 FROM users WHERE id=?', (target,)).fetchone(), 'Choose an existing employee', 422)
                db.execute('UPDATE skills SET owner_id=? WHERE id=?', (target, sid))
                # A new owner must resubmit pending work to an independent reviewer.
                db.execute("UPDATE versions SET state='draft',reviewer_id=NULL,approved_hash=NULL,approved_revision=NULL,revision=revision+1 WHERE skill_id=? AND state IN ('draft','in_review','approved')", (sid,))
                reason = 'Ownership transferred to ' + target
            else:
                raise HubError(422, "Unknown skill action")
            db.execute("UPDATE skills SET revision=revision+1,updated_at=? WHERE id=?", (now(), sid))
            self.event(db, user, action, sid, target, reason)
        return {"ok": True}

    def diff(self, vid, user):
        with self.tx() as db:
            s, v = self.version_row(db, vid, user)
            before = {}
            if s["current_id"] and s["current_id"] != vid:
                _, old = self.version_row(db, s["current_id"], user)
                before = old["files"]
            changes = []
            for path in sorted(before.keys() | v["files"].keys()):
                if before.get(path) == v["files"].get(path):
                    continue
                a, b = self.object_get(before[path]) if path in before else b"", self.object_get(v["files"][path]) if path in v["files"] else b""
                try:
                    require(len(a) + len(b) < 200000, "large", 422)
                    text = "".join(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True), fromfile="previous/"+path, tofile="draft/"+path))
                except (UnicodeError, HubError):
                    text = "Binary or large file changed. Download to inspect."
                changes.append({"path": path, "status": "added" if path not in before else "removed" if path not in v["files"] else "changed", "diff": text})
            return changes

    def catalog(self, user, sid=None):
        with self.tx() as db:
            result = []
            for s in db.execute("SELECT * FROM skills WHERE deleted_at IS NULL AND current_id IS NOT NULL").fetchall():
                if sid and s["id"] != sid:
                    continue
                if s["visibility"] != "company" and s["owner_id"] != user["id"]:
                    continue
                v = db.execute("SELECT * FROM versions WHERE id=?", (s["current_id"],)).fetchone()
                if not v or v["state"] not in ("published", "deprecated"):
                    continue
                retirement = db.execute("SELECT detail FROM audit WHERE version_id=? AND action='deprecate' ORDER BY id DESC LIMIT 1", (v['id'],)).fetchone()
                result.append({**dict(s), "version_id": v["id"], "version": v["label"], "package_hash": v["package_hash"], "state": v["state"], "retirement_note": retirement[0] if retirement else None, "meta": json.loads(v["meta"]), "inventory": json.loads(v["files"]), "owner_name": db.execute("SELECT name FROM users WHERE id=?", (s["owner_id"],)).fetchone()[0]})
            if sid:
                require(result, "No accessible current release", 404)
                return result[0]
            return result

    def intent(self, user, sid, client):
        require(client in CLIENTS, "Unknown client", 422)
        current = self.catalog(user, sid)
        fingerprint = digest(json.dumps({"dependencies": current["meta"].get("dependencies", []), "compatibility": current["meta"].get("compatibility", {}).get(client)}, sort_keys=True).encode())
        with self.tx() as db:
            r = db.execute("SELECT * FROM intents WHERE user_id=? AND client=? AND skill_id=? AND version_id=? AND hash=? AND expires>? ORDER BY rowid DESC LIMIT 1", (user["id"], client, sid, current["version_id"], current["package_hash"], time.time())).fetchone()
            if r:
                return {**dict(r), "release": current}
            grant = db.execute("SELECT * FROM grants WHERE user_id=? AND client=? AND skill_id=?", (user["id"], client, sid)).fetchone()
            approved = grant and grant["fingerprint"] == fingerprint and (grant["version_id"] == current["version_id"] or grant["auto_update"])
            ident = uid()
            db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)", (ident, user["id"], client, sid, current["version_id"], current["package_hash"], "approved" if approved else "pending", grant["auto_update"] if approved else 0, time.time()+600))
            r = db.execute("SELECT * FROM intents WHERE id=?", (ident,)).fetchone()
            return {**dict(r), "release": current}

    def decide_intent(self, user, ident, approve, auto_update=False):
        with self.tx() as db:
            r = db.execute("SELECT * FROM intents WHERE id=? AND user_id=? AND expires>?", (ident, user["id"], time.time())).fetchone()
            require(r, "Installation request not found or expired", 404)
            require(r["status"] == "pending", "Installation request is already decided")
            v = db.execute("SELECT * FROM versions WHERE id=?", (r["version_id"],)).fetchone()
            meta = json.loads(v["meta"])
            fingerprint = digest(json.dumps({"dependencies": meta.get("dependencies", []), "compatibility": meta.get("compatibility", {}).get(r["client"])}, sort_keys=True).encode())
            db.execute("UPDATE intents SET status=?,auto_update=? WHERE id=?", ("approved" if approve else "denied", int(auto_update), ident))
            if approve:
                db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?,?,?)", (user["id"], r["client"], r["skill_id"], r["version_id"], r["hash"], fingerprint, int(auto_update)))
            self.event(db, user, "install_approved" if approve else "install_denied", r["skill_id"], r["version_id"], r["client"])
        return {"ok": True}

    def intent_package(self, user, ident):
        with self.tx() as db:
            r = db.execute("SELECT * FROM intents WHERE id=? AND user_id=? AND expires>?", (ident, user["id"], time.time())).fetchone()
            require(r, "Installation request not found or expired", 404)
            require(r["status"] == "approved", "Human installation approval is required", 403)
            r = dict(r)
        release = self.catalog(user, r["skill_id"])
        require(release["version_id"] == r["version_id"] and release["package_hash"] == r["hash"], "Release changed; prepare again")
        return self.object_get(r["hash"])
