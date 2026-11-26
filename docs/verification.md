# MVP Verification

The system design was written and pushed before implementation. This record distinguishes the working local trial from a company deployment.

## Automated checks

* HTTP workflow tests exercise private access by direct URL, draft and asset editing, conflicting writes, independent review, invalid transitions, immutable publication, private historical versions, visibility reduction, archive, deletion/restore, CSRF and loopback restrictions.
* Connector tests exercise no download before approval, declined installs, cache integrity repair, updates, renewed approval for changed dependencies, offline failure, and the configuration shapes for Codex, Cursor, and VS Code Copilot.
* OIDC callback tests use signed test identity tokens and an isolated simulated provider. They cover state replay, nonce, signature, verified email, audience, expiration, and successful session creation. They do not verify a company's actual provider or role policy.
* Live MCP tests initialize real stdio sessions and use the running local API for each of the three client configurations. They discover tools, list CCDI, request and receive browser-session approval, download its complete package, and inspect the installed version. Temporary tokens are revoked afterward.
* A Playwright browser check creates and edits a skill, changes the requested audience, submits it, switches to an independent reviewer, approves, switches back, publishes, and inspects the real CCDI workbench. It also checks the three connection panels and a narrow mobile layout without horizontal overflow. Only its synthetic skill is deleted afterward.
* The portable CCDI builder is checked for native editable text/shapes, retained logos, slide count, and speaker notes. A two-slide sample was rendered through LibreOffice and visually inspected. Local font availability affects exact typography; a renderer with Montserrat installed is required for final brand verification.

Run the commands in the README to reproduce these checks. Browser screenshots and sample decks are saved under ignored `artifacts/`. The local reference deck is intentionally not committed; tests that need it skip when the company source is absent.

## Limits that remain

The MCP transport and generated configuration are verified; model selection inside the installed Codex, Cursor, and GitHub Copilot applications is a separate user trial. No existing global client configuration was replaced. Company OIDC credentials and a deployed HTTPS origin have not been supplied, so company sign-in is not claimed as tested. The running local portal uses conspicuously labeled demonstration identities.

Postman CLI was available but could not access the native credential store in this environment. No Postman login or cloud publication was attempted. The recorded evidence comes from the HTTP tests, official MCP SDK sessions, and browser workflow above.

Private files and previously loaded model context cannot be recalled. The connector checks fresh access and release state before returning its managed cache, but direct use of a personal copy can bypass this flow. SQLite and private local blob storage are intended for one server; PostgreSQL and shared private storage are required before scaling to multiple replicas.
