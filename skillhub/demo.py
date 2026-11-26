"""Generic sample content for the explicitly requested local demonstration."""
from .app import create_app
from .core import CLIENTS, parse_skill, require


SAMPLES = (
    (
        "project-briefing-demo", "Project Briefing (Demo)", "company",
        "Demo skill: turn project notes into a concise status briefing with evidence, risks, and next steps.",
        """# Project briefing

Use the source notes supplied by the employee. Follow the [brief template](references/template.md).
Separate confirmed facts from open questions. Preserve dates and link to source evidence where available.
List each blocker with its impact and an owner only when one is supplied. Do not invent progress or deadlines.
Return a draft for review; sending or publishing it requires the employee's instruction.

This is editable demonstration content, not an organization policy.
""",
        {"references/template.md": b"# Project briefing\n\n## Status\n\n## Evidence\n\n## Risks and blockers\n\n## Next steps\n\n## Open questions\n"},
    ),
    (
        "code-review-demo", "Code Review Checklist (Demo)", "company",
        "Demo skill: review a proposed code change for correctness, access control, and missing regression coverage.",
        """# Code review checklist

Read the change, its surrounding code, and repository instructions. Identify concrete defects introduced by the change.
For each finding, explain a reproducible trigger, impact, and the relevant file and line.
Prioritize data loss, authorization errors, and incorrect behavior. Distinguish observed failures from untested concerns.
Report checks actually run and any remaining gaps. If there are no findings, say so.
Return review comments as a draft; do not post comments, merge changes, or modify access without authorization.

This is editable demonstration content, not an organization policy.
""", {},
    ),
    (
        "personal-writing-demo", "Personal Writing Notes (Demo)", "private",
        "Demo personal skill: revise a draft into clear, concise prose while preserving its meaning.",
        """# Personal writing notes

Read the supplied draft and identify its audience and purpose. Preserve facts, names, commitments, and uncertainty.
Use short sentences, active voice, and concrete language. Remove repetition without changing the author's intent.
Return the revised draft and briefly identify any material ambiguity. Do not send or publish it.

This personal draft is editable sample content. Replace these preferences with your own.
""", {},
    ),
)


def seed_demo(root="data"):
    app = create_app(root)
    require(app.state.mode == "demo", "Sample skills can only be seeded in demo mode", 403)
    store = app.state.store
    users = {u["email"]: u for u in store.users()}
    author, reviewer = users["author@example.test"], users["reviewer@example.test"]
    # Include deleted and edited samples so repeating setup cannot resurrect or overwrite them.
    with store.tx() as db:
        existing = {row[0] for row in db.execute("SELECT json_extract(meta, '$.demo_sample') FROM versions")}
    created_ids = []
    for slug, name, audience, description, instructions, resources in SAMPLES:
        if slug in existing:
            continue
        files = {"SKILL.md": f"---\nname: {slug}\ndescription: {description!r}\n---\n\n{instructions}".encode(), **resources}
        parse_skill(files)
        meta = {"display_name": name, "demo_sample": slug, "provenance": "Built-in generic demonstration content", "compatibility": {c: "supported" for c in CLIENTS}}
        skill = store.create(author, name, description, files, meta)
        created_ids.append(skill["id"])
        if audience == "private":
            continue
        v = skill["versions"][0]
        skill = store.edit(v["id"], author, v["revision"], changes={"visibility": audience, "changelog": "Generic demo fixture; replace or independently review before company rollout."})
        v = skill["versions"][0]
        skill = store.transition(v["id"], author, "submit", v["revision"], reviewer_id=reviewer["id"])
        v = skill["versions"][0]
        skill = store.transition(v["id"], reviewer, "approve", v["revision"], "Demonstration fixture approval, not company approval.")
        v = skill["versions"][0]
        store.transition(v["id"], author, "publish", v["revision"])
    return created_ids
