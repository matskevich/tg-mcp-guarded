# Repository Guidelines

## Project Structure & Modules
`tganalytics/tganalytics` holds the Python package: `infra/` wraps Telegram clients plus rate-limiters, `domain/` manages group/member logic, and `config/` centralizes defaults consumed by `mcp_server.py`. Supporting assets include `scripts/` (env utilities, compliance, audits), `docs/` (anti-spam and security notes), and `tests/` which mirrors the production folders (`tests/core` stresses the limiter). Runtime artifacts under `data/` (`sessions`, `export`, `anti_spam`, `logs`) stay local only—run `make setup-dirs` or `make dev-setup` to create them with the right permissions.

## Build, Test, and Development Commands
- `python3 -m venv venv && source venv/bin/activate` followed by `pip install -r requirements.txt` or `make install` primes dependencies.
- `cp .env.sample .env` or `make sync-env` keeps credentials synchronized; `make dev-setup` chains env sync, dependency install, and directory creation.
- `PYTHONPATH=tganalytics:. python -m pytest tests/ -q` (also `make test`) runs the full suite; `make test-fast` skips `slow` markers and `make test-limiter` isolates bucket math.
- `make anti-spam-check`, `make telegram-api-audit`, and `make security-check` enforce safe API usage and security scanning.
- `make dev-check` performs formatting + linting + compliance before committing.

## Coding Style & Naming Conventions
Use 4-space indentation, snake_case modules, PascalCase classes, and uppercase env constants. `black`, `isort`, and `flake8 --max-line-length=100 --ignore=E203,W503` govern formatting, while `bandit` and `yamllint` raise safety issues (see `.pre-commit-config.yaml`). MCP tool functions must follow the `tg_<verb>` naming scheme so CLI help, filenames, and docs stay aligned.

## Testing Guidelines
Pytest discovery is defined in `pytest.ini` (files `test_*.py`, classes `Test*`, functions `test_*`). Decorate async tests with `@pytest.mark.asyncio` and flag heavy tests with `slow` or `integration` so contributors can rely on `make test-fast`. Expand fixtures inside `tests/conftest.py`, cover every new Telegram RPC with limiter/anti-spam cases, and remember CI runs `python -m pytest tests/ -q` with `PYTHONPATH=tganalytics:.`.

## Security & Configuration Tips
Route every Telegram client call through `_safe_api_call`/`safe_call` and verify with `python scripts/check_anti_spam_compliance.py`. Keep `.env` minimal, add new keys to `.env.sample`, and guard secrets by leaving `data/sessions` out of Git (permissions handled by `make setup-dirs`). Use `make check-env` before running locally and document rate-limit changes inside `docs/ANTISPAM_SECURITY.md`.

## Telegram Write Policy (Mandatory)
- Telegram write operations must go only through `tgmcp-actions` tools.
- Direct telethon write (`client.send_message`, `client.send_file`, `client.delete_messages`, etc.) is blocked by default by `tele_client` write guard.
- Raw MTProto write via `client(Request)` (invite/add/remove/ban/edit/send) is also blocked unless running as Action MCP process.
- For write actions, keep `TG_ACTIONS_REQUIRE_ALLOWLIST=1` and use explicit targets in `TG_ACTIONS_ALLOWED_GROUPS`.
- Non-dry-run action calls require `confirm=true`, exact `confirmation_text`, and one-time `approval_code` from matching `dry_run`.
- Duplicate write actions are blocked by idempotency window unless `force_resend=true`.
- For multi-step migrations, prefer batch flow: `tg_create_add_member_batch` -> `tg_approve_batch` -> `tg_run_add_member_batch`.
- Agent onboarding docs:
- `docs/AGENT_START.md` for the bootstrap prompt, portable skills and readiness criteria
- `docs/RECIPES.md` for bounded reads, delivery read-back, assistant voice and media workflows
- `docs/AGENT_PLAYBOOK.md` for canonical agent execution flows
- `docs/ACTION_POLICY_TOGGLES.md` for safe env toggles and allowlist changes

## Commit & Pull Request Guidelines
We use lightweight Conventional Commits (`feat:`, `fix:`, `refactor:`) and granular commits aid review. A pull request should link issues, note risk/rollout, attach `make test` + audit output, and include logs or screenshots for MCP-visible changes. Update docs/tests when behavior shifts, mention env changes up front, and wait for both CI workflows (CI + Anti-spam Compliance) before requesting review.
