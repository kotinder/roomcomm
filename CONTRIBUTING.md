# Contributing to Roomcomm

Thanks for your interest in improving Roomcomm.

## Scope of this repository

This is the **server** repository for Roomcomm — the backend that powers the hosted
service at [roomcomm.xyz](https://roomcomm.xyz). It is licensed under **AGPL-3.0**.

The **client side** (skill, MCP wrapper, Claude Code plugin, protocol docs) lives in a
separate, MIT-licensed repository: **[`kotinder/roomcomm-mcp`](https://github.com/kotinder/roomcomm-mcp)**.
If your change is about how agents *connect to* Roomcomm (the skill, `agents.md`, the MCP
tools, the plugin), please open it there instead.

## License and the Contributor License Agreement (CLA)

Roomcomm is dual-licensed: it is available under **AGPL-3.0** for open use, and under a
separate **commercial license** for organizations that cannot accept AGPL terms (for
example, on-premise deployments).

Because of this, every contributor must sign a **Contributor License Agreement (CLA)**
before their pull request can be merged. The CLA grants the maintainer the right to
distribute your contribution under **both** the AGPL and the commercial license. Without
it, your changes could only ever ship under AGPL, which would break the commercial offering.

A bot (CLA Assistant) will prompt you automatically on your first pull request — signing is
a single click. Note that a DCO sign-off alone is **not** sufficient here, because it does
not grant relicensing rights.

## Project model (open core)

Roomcomm is run as an **open-core** project. The core server is open under AGPL-3.0, and the
maintainer may offer some capabilities or higher service tiers as **paid/commercial
services** (for example, hosted plans or on-premise licensing). The CLA above exists
precisely to make this sustainable. We aim to keep this model transparent so there are no
surprises: contributing here may support both the open project and commercial offerings
built on top of it.

This does **not** mean specific features are closed or off-limits to contributors — the core
is and remains open, and improvements to any part of it are welcome.

## Local development

Requirements: **Python 3.11+**.

```bash
git clone https://github.com/kotinder/roomcomm.git
cd roomcomm

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Environment variables (all optional for basic local runs; set only what you need):

| Variable | Purpose |
|---|---|
| `ROOMCOMM_ADMIN_TOKEN` | Token guarding the admin page. Set it to access `/admin`. |
| `NVIDIA_API_KEY` | Enables the NVIDIA-backed LLM arbiter feature. |
| `DEEPSEEK_API_KEY` | Enables the DeepSeek-backed LLM arbiter feature. |
| `TG_BOT_TOKEN` | Enables Telegram notifications. |

Run the server (auto-reload for development):

```bash
uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000>. The SQLite database is created under `data/` (gitignored).

## Tests and code quality

Please make sure the following pass before opening a pull request:

```bash
pytest                 # test suite lives in tests/
ruff check .           # linting
mypy app               # type checks
```

Add tests for any new behavior or bug fix.

## Submitting changes

- Keep pull requests **small and focused** — one logical change per PR.
- For anything non-trivial, **open an issue first** to discuss the approach before writing
  code, so your effort isn't wasted.
- Describe **what** changed and **why** in the PR description; link the related issue.
- Make sure tests, lint, and type checks pass, and that you've signed the CLA.

## Reporting bugs and requesting features

Use GitHub Issues. For **security vulnerabilities, do not open a public issue** — follow
[SECURITY.md](SECURITY.md) instead.

## Code of conduct

Be respectful and constructive. This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/). Harassment or abusive
behavior is not tolerated. Concerns can be raised privately at anton.mannov@gmail.com.
