import argparse
import json
import os
import sys
from pathlib import Path

from .app import create_app
from .core import HubError, parse_skill, require


FINDER = """---
name: company-skill-finder
description: Discover approved company workflows for specialized tasks, including CCDI presentations. Use before starting a task that may have an organizational workflow.
---

Use company_skill_hub list_skills to inspect available company workflows. Select a skill only when its description fits the request. Call prepare_skill for the selected ID before every use, including follow-up work; the connector checks the approved version and access policy. If approval is pending, show the employee the approval link and wait for their decision. Do not make the decision for the employee. When ready, read the returned SKILL.md and supporting files using the returned directory as the base. Respect the current user's instructions and existing permissions. If a dependency or connection is unavailable, explain it and offer to proceed without the company skill. Do not read a remembered cached path without preparing it again.
"""


def seed(source, root="data", portable=False):
    app = create_app(root)
    require(app.state.mode == "demo", "Automatic demonstration seeding is disabled outside demo mode", 403)
    store = app.state.store
    users = {u["email"]: u for u in store.users()}
    author, reviewer = users["author@example.test"], users["reviewer@example.test"]
    existing = next((s for s in store.listing(author) if s["name"] == "CCDI Slide Generation"), None)
    if existing and not portable:
        print("CCDI demonstration is already seeded")
        return
    files = {str(p.relative_to(source)): p.read_bytes() for p in Path(source).rglob("*") if p.is_file() and not p.is_symlink()}
    require(files and "SKILL.md" in files, "Source must be the complete CCDI skill directory", 422)
    meta = {"display_name": "CCDI Slide Generation", "dependencies": ["capability:presentations"], "compatibility": {"codex": "requires_setup", "cursor": "requires_adapter", "copilot": "requires_adapter"}}
    if portable:
        from .portable import convert
        files, meta = convert(files)
    if existing:
        from .core import digest
        previous = store.detail(existing['id'], author)['versions']
        if previous[0]['meta'].get('workflow') == 'portable-editable-pptx' and previous[0]['files'].get('scripts/build_deck.py') == digest(files['scripts/build_deck.py']):
            print('Portable CCDI demonstration is already up to date')
            return
        latest = max(tuple(map(int,v['label'].split('.'))) for v in previous)
        label = f'{latest[0]}.{latest[1]}.{latest[2]+1}' if previous[0]['meta'].get('workflow') else '1.1.0'
        created = store.new_version(existing['id'], author, label)
        v = created['versions'][0]
        for path, content in files.items():
            created = store.edit(v['id'], author, v['revision'], path=path, data=content)
            v = created['versions'][0]
        created = store.edit(v['id'], author, v['revision'], changes={'meta':meta})
    else:
        created = store.create(author, "CCDI Slide Generation", parse_skill(files)["description"], files, meta)
    sid, v = created["id"], created["versions"][0]
    created = store.edit(v["id"], author, v["revision"], changes={"visibility": "company", "changelog": "Demonstration import. Source assets remain local. Independent company review is required before rollout."})
    v = created["versions"][0]
    created = store.transition(v["id"], author, "submit", v["revision"], reviewer_id=reviewer["id"])
    v = created["versions"][0]
    created = store.transition(v["id"], reviewer, "approve", v["revision"], "Demonstration fixture approval, not company approval.")
    v = created["versions"][0]
    store.transition(v["id"], author, "publish", v["revision"])
    print("Imported and published local CCDI demonstration: "+sid)


def main():
    parser = argparse.ArgumentParser(description="Company Skill Hub")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    setup = sub.add_parser("seed")
    setup.add_argument("--source", type=Path, required=True)
    setup.add_argument("--portable", action="store_true")
    setup.add_argument("--data", default=os.getenv("HUB_DATA", "data"))
    configure = sub.add_parser("config")
    configure.add_argument("client", choices=("codex", "cursor", "copilot"))
    configure.add_argument("--url", default="http://127.0.0.1:8765")
    configure.add_argument("--token-file", required=True)
    configure.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        require(os.getenv("HUB_AUTH_MODE", "demo") != "demo" or args.host in ("127.0.0.1", "localhost", "::1"), "Demo mode can bind only to loopback", 403)
        os.environ.setdefault("HUB_BASE_URL", f"http://127.0.0.1:{args.port}")
        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.command == "seed":
        seed(args.source, args.data, args.portable)
    else:
        from .connector import config
        text = config(args.client, args.url, args.token_file)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            filename = "codex.toml" if args.client == "codex" else "mcp.json"
            (args.output/filename).write_text(text)
            finder = args.output/"skills"/"company-skill-finder"
            finder.mkdir(parents=True, exist_ok=True)
            (finder/"SKILL.md").write_text(FINDER)
            guidance = "# Company skill discovery\n\n"+FINDER.split("---", 2)[-1].strip()+"\n"
            if args.client == "cursor":
                (args.output/"company-skills.mdc").write_text("---\ndescription: Company skill discovery\nalwaysApply: true\n---\n"+guidance)
            else:
                (args.output/("AGENTS.md" if args.client == "codex" else "copilot-instructions.md")).write_text(guidance)
            print("Generated integration files in "+str(args.output.resolve()))
        else:
            print(text)


if __name__ == "__main__":
    try:
        main()
    except HubError as e:
        sys.exit(str(e))
