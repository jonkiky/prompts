# Company Skill Hub

An internal management portal for reusable agent skills, with a local MCP connector for **Codex, Cursor, and GitHub Copilot in VS Code**. Create or import ZIP packages, edit files, request independent review, publish Personal or Organization releases, manage versions, and retire or restore skills. The black-and-white portal uses compact navigation, a searchable skill table, and a split editing workspace inspired by Langfuse.

The [system design](docs/system-design.md) was committed before application implementation. [OpenAPI](docs/openapi.json) describes the HTTP interface; `/docs` exposes the same contract while running.

## Run locally

Requires Python 3.11 or later. From this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m skillhub serve
```

Open [the local portal](http://127.0.0.1:8765). Demo mode binds only to loopback and offers four sample identities. Jordan is the author, Morgan the reviewer, Sam the administrator, and Riley another employee. Demonstration decisions are not company authorization.

For generic sample content alongside CCDI, run:

```sh
.venv/bin/python -m skillhub seed-demo
```

This adds **Project Briefing (Demo)** and **Code Review Checklist (Demo)** at the Organization level, plus **Personal Writing Notes (Demo)** as Jordan's Personal draft. Repeating the command preserves edited and deleted samples. It is disabled outside demo mode and does not require the company CCDI assets.

## Import the first CCDI skill

The retained reference deck and original assets stay outside the source repository. Import the complete local source directory:

```sh
.venv/bin/python -m skillhub seed --source /absolute/path/to/artifact-template-ccdi-slide-generation --portable
```

This demo-only command publishes an auditable demonstration release. If the original was already imported, it adds a new portable edition while retaining the original. To exercise real approval manually, use **Import ZIP** in the portal instead. Original package content, application data, tokens, and generated integration files are ignored by Git.

The portable CCDI edition bundles an editable PowerPoint builder using `python-pptx`, plus the original style guide, reference deck, and preview assets. It preserves the reference header logos and cover photography, and generates editable text and diagram shapes. Actual selection by each AI client still needs a client trial. The portable builder is an explicit alternative to the original OpenAI Presentations dependency; it does not impersonate that plugin. See [verification](docs/verification.md) for what has actually been tested.

## Connect the three AI tools

1. Sign in as the employee who will use the connector. Open **Connections** and generate a personal token. It is shown once.
2. Save it to a local file outside shared source control and restrict permissions (`chmod 600 /path/to/token` on macOS/Linux). Do not put the token in a prompt or committed configuration.
3. Generate the configuration for each tool:

```sh
.venv/bin/python -m skillhub config codex --token-file /path/to/token --output .integrations/codex
.venv/bin/python -m skillhub config cursor --token-file /path/to/token --output .integrations/cursor
.venv/bin/python -m skillhub config copilot --token-file /path/to/token --output .integrations/copilot
```

Generated paths point to the current Python interpreter and repository checkout. Regenerate after moving the installation. They contain a token **file path**, never the token.

* **Codex:** merge `codex.toml` into the user's MCP configuration. Merge the supplied company guidance into an appropriate `AGENTS.md`, or install the finder skill in an enabled skills directory. Preserve existing instructions.
* **Cursor:** merge the generated `mcp.json` server into user or project MCP configuration. Place `company-skills.mdc` in `.cursor/rules/` as an always-applied company rule.
* **VS Code GitHub Copilot:** merge the generated `servers` entry into VS Code's MCP configuration (`.vscode/mcp.json` for a project). Merge company guidance into `.github/copilot-instructions.md`. Use Copilot agent mode and verify the selected harness supports the configuration.

Reload the relevant client and verify **Company Skill Hub** exposes `list_skills`, `prepare_skill`, and `get_status`. The configuration generator deliberately leaves existing global settings untouched. Discovery is model-driven and must be tested separately from an MCP protocol connection.

First preparation returns an approval link. The matching employee must approve it in the portal, then the assistant calls prepare again. Only then does the connector download the package. Future uses recheck server access, release status, compatibility, and file hashes. Updates follow the employee's saved policy; changed dependencies require renewed consent. A bearer connector token cannot approve its own installation or manage catalog content.

## Management workflow

Create or ZIP-import → edit the private draft → select a reviewer → submit → approve as a different user → publish the exact approved revision. Files, release notes, dependency metadata, and audience are bound to that approval. Reopening a review invalidates approval. Published files are immutable; **New version** starts a separate draft while the current release remains available.

**Personal** skills are private to their owner, authorized reviewers, and administrators. **Organization** releases are shared with authenticated employees after reviewed publication. Use the level tabs in the library to filter either group; **My skills** includes everything you own. The API retains `private` and `company` audience values for compatibility. Drafts request their next level in **Settings → Requested skill level**; that request does not change access before publication. Published Personal releases are discoverable by their owner through the connector.

**Make personal now** narrows access immediately. Version history supports inspection, ZIP export, deprecation, and archiving. There is no rollback action. Publish a new reviewed version when content needs to change. Deleting is soft; an administrator can find deleted skills under Lifecycle and restore them. Narrowing access cannot recall files already downloaded or instructions already in a conversation.

## Authentication and deployment

The working local default is demo authentication. For company sign-in, configure the variables in `.env.example` with **OIDC mode**, an HTTPS base URL, issuer, client ID/secret, verified email domain, and initial reviewer/admin allowlists. Register `/auth/callback` on the identity provider. The application verifies authorization state, PKCE, nonce, signature, issuer, audience, and token lifetime. Existing accounts are bound to issuer and subject. Company provider configuration and its live login flow must be verified before rollout.

Use a TLS reverse proxy and persistent private `HUB_DATA` volume. Keep credentials in process secret configuration; `.env.example` is a reference, not an automatically loaded secrets file. Do not expose demo mode publicly. Role changes are available to administrators; connector tokens expire after 30 days and can be revoked.

The initial single-server database is SQLite. Source blobs and release ZIPs are content-addressed files under `HUB_DATA/objects`, outside the web root. Back up the stopped database and object directory together; restore into a new data directory before restarting. Multiple replicas require PostgreSQL and shared private storage. Uploaded scripts are never run by the server. Text/HTML/SVG assets cannot execute in the portal origin.

## Verification

```sh
.venv/bin/python -m pytest -q
PYTHONPATH=. .venv/bin/python scripts/export_openapi.py
HUB_LIVE_URL=http://127.0.0.1:8765 .venv/bin/python -m pytest tests/test_live_mcp.py -q
```

The last command requires a local running demo with a portable CCDI release. It exercises real stdio MCP sessions for each client configuration and revokes the temporary verification tokens afterward. See [verification](docs/verification.md) for browser evidence, known limits, and the difference between protocol tests and tests inside the installed AI products.

Set `CCDI_SKILL_SOURCE` to your local reference skill directory when running tests that validate the real CCDI package and portable slide builder. Without it, those two company-asset tests skip. For the optional browser check, install Node.js and Chrome, run `npm install`, then `npm run test:browser` against the local demonstration server. It uses an isolated browser profile and deletes only its own synthetic skill afterward.
