"""Local stdio MCP adapter. No global client settings are modified."""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from .core import CLIENTS, HubError, MAX_ZIP, digest, require, unpack


class Connector:
    def __init__(self, url, token, client, cache):
        parsed = urlparse(url)
        require(parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")), "Use HTTPS for a remote hub", 422)
        require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment, "Invalid hub URL", 422)
        require(client in CLIENTS, "Unknown client", 422)
        self.url, self.token, self.client, self.cache = url.rstrip("/"), token, client, Path(cache)
        self.http = httpx.Client(base_url=self.url, headers={"Authorization": "Bearer "+token}, timeout=20, trust_env=False, follow_redirects=False)

    def request(self, method, path, **kwargs):
        try:
            r = self.http.request(method, path, **kwargs)
            if r.status_code >= 400:
                try:
                    message = r.json().get("detail", "Hub request failed")
                except ValueError:
                    message = "Hub request failed"
                raise HubError(r.status_code, str(message))
            return r
        except httpx.HTTPError as e:
            raise HubError(503, "Cannot verify the current company release. Retry when the hub is available.") from e

    def list_skills(self, query=""):
        releases = self.request("GET", "/api/catalog").json()
        # Metadata is matched locally. Task text is never sent to the hub.
        words = query.lower().split()
        if words:
            releases.sort(key=lambda r: sum(w in (r["name"]+" "+r["description"]).lower() for w in words), reverse=True)
        return [{k: r[k] for k in ("id", "name", "description", "version", "owner_name", "state", "meta")} for r in releases]

    def prepare_skill(self, skill_id):
        release = self.request("GET", "/api/catalog/"+skill_id).json()
        compatibility = release["meta"].get("compatibility", {}).get(self.client, "unverified")
        missing = []
        capabilities = os.getenv("HUB_CAPABILITIES", "").split(",")
        for dep in release["meta"].get("dependencies", []):
            if dep.startswith("python:") and importlib.util.find_spec(dep[7:]) is None:
                missing.append(dep)
            elif dep.startswith("capability:") and dep[11:] not in capabilities:
                missing.append(dep)
            elif dep.startswith("command:") and not shutil.which(dep[8:]):
                missing.append(dep)
        if missing or compatibility in ("unsupported", "requires_adapter"):
            return {"status": "needs_setup", "skill": release["name"], "version": release["version"], "missing": missing, "compatibility": compatibility, "note": "Resolve these prerequisites before applying this skill."}
        intent = self.request("POST", "/api/install-intents", json={"skill_id": skill_id, "client": self.client}).json()
        if intent["status"] != "approved":
            return {"status": intent["status"], "approval_url": intent["approval_url"], "skill": release["name"], "version": release["version"], "note": "The matching employee must decide in the portal. No package was downloaded. Call prepare_skill again after approval."}
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.cache / (skill_id+".lock")
        try:
            lock.mkdir()
        except FileExistsError as e:
            raise HubError(409, "Another install is active. Retry; remove an abandoned lock only after confirming the connector is stopped.") from e
        try:
            dest = self.cache / skill_id / release["package_hash"]
            inventory = release["inventory"]
            good = dest.is_dir() and not dest.is_symlink()
            if good:
                actual = {str(p.relative_to(dest)): digest(p.read_bytes()) for p in dest.rglob("*") if p.is_file() and not p.is_symlink()}
                good = actual == inventory and not any(p.is_symlink() for p in dest.rglob("*"))
            if not good:
                data = self.request("GET", "/api/install-intents/"+intent["id"]+"/package").content
                require(len(data) <= MAX_ZIP and digest(data) == release["package_hash"], "Package integrity check failed", 422)
                files = unpack(data)
                require({p: digest(b) for p, b in files.items()} == inventory, "Package inventory does not match release", 422)
                dest.parent.mkdir(parents=True, exist_ok=True)
                require(not dest.parent.is_symlink(), "Cache directory cannot be a symlink", 422)
                with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
                    stage = Path(tmp) / "package"
                    stage.mkdir()
                    for path, content in files.items():
                        target = stage / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                    if dest.is_symlink():
                        dest.unlink()
                    elif dest.exists():
                        shutil.rmtree(dest)
                    os.replace(stage, dest)
            record = {"skill_id": skill_id, "version": release["version"], "hash": release["package_hash"], "path": str(dest.resolve()), "last_checked": time.time(), "client": self.client}
            # Per-skill records avoid losing another skill's installation on concurrent updates.
            tmp_record = self.cache / (skill_id+".json.tmp")
            tmp_record.write_text(json.dumps(record, indent=2))
            os.replace(tmp_record, self.cache / (skill_id+".json"))
            return {"status": "ready", **record, "skill_file": str(dest.resolve()/"SKILL.md"), "python": sys.executable, "lifecycle": release["state"], "retirement_note": release.get('retirement_note'), "note": "Read the local SKILL.md and supporting files. Installation grants no permission for external actions."}
        finally:
            lock.rmdir()

    def get_status(self):
        if not self.cache.exists():
            return []
        return [json.loads(p.read_text()) for p in self.cache.glob("*.json")]


def config(client, url, token_file, cache=None):
    require(client in CLIENTS, "Unknown client", 422)
    env = {"HUB_URL": url, "HUB_CLIENT": client, "HUB_TOKEN_FILE": str(Path(token_file).resolve()), "HUB_CACHE": str(Path(cache or ".hub-cache").resolve()), "PYTHONPATH": str(Path(__file__).parent.parent.resolve())}
    entry = {"command": sys.executable, "args": ["-m", "skillhub.connector"], "env": env}
    if client == "codex":
        lines = ["[mcp_servers.company_skill_hub]", "command = "+json.dumps(entry["command"]), 'args = ["-m", "skillhub.connector"]', "[mcp_servers.company_skill_hub.env]"]
        return "\n".join(lines+[k+" = "+json.dumps(v) for k, v in env.items()])+"\n"
    return json.dumps({"servers" if client == "copilot" else "mcpServers": {"company_skill_hub": {**({"type": "stdio"} if client == "copilot" else {}), **entry}}}, indent=2)+"\n"


def main():
    token_path = Path(os.environ["HUB_TOKEN_FILE"])
    require(not token_path.is_symlink() and (os.name == "nt" or not token_path.stat().st_mode & 0o077), "Token file must be private to its owner (chmod 600)", 403)
    connector = Connector(os.environ["HUB_URL"], token_path.read_text().strip(), os.environ["HUB_CLIENT"], os.getenv("HUB_CACHE", str(Path.home()/".company-skill-hub")))
    server = FastMCP("Company Skill Hub", instructions="Discover approved company workflows for a relevant task, then prepare the selected skill. Only the employee can approve installation in the linked portal.")

    def result(fn, *args):
        try:
            return fn(*args)
        except (HubError, OSError, ValueError) as e:
            raise ValueError(str(e)) from e

    @server.tool()
    def list_skills(query: str = "") -> list[dict] | dict:
        """Find approved company skills relevant to a task. Matching uses local metadata."""
        return result(connector.list_skills, query)

    @server.tool()
    def prepare_skill(skill_id: str) -> dict:
        """Check the current approved version, request human consent, then install verified files."""
        return result(connector.prepare_skill, skill_id)

    @server.tool()
    def get_status() -> list[dict] | dict:
        """Show versions installed by this connector and the time they were last checked."""
        return result(connector.get_status)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
