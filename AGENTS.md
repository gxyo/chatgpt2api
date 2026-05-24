# Repository Guidelines

## Project Structure & Module Organization

This repository combines a FastAPI backend with a Next.js management UI. Backend entrypoint is `main.py`, with API routers in `api/`, business logic in `services/`, shared helpers in `utils/`, migrations and utilities in `scripts/`, and regression tests in `test/`. Frontend code lives in `web/`: routes are under `web/src/app/`, reusable UI in `web/src/components/`, and client helpers/state in `web/src/lib/` and `web/src/store/`. Static screenshots and README images are in `assets/`; longer notes are in `docs/`.

## Build, Test, and Development Commands

- `uv sync`: install Python dependencies from `pyproject.toml` and `uv.lock`.
- `uv run main.py`: run the local backend, normally serving API traffic on `localhost:8000`.
- `python -m unittest discover test`: run backend tests in `test/`.
- `cd web && bun install`: install frontend dependencies from `web/bun.lock`.
- `cd web && bun run dev`: start the Next.js dev server on all interfaces.
- `cd web && bun run build`: build the frontend for production.
- `docker compose up -d`: run the self-hosted stack with the root Compose file.

## Coding Style & Naming Conventions

Use Python 3.13 features only when they improve clarity. Keep backend modules snake_case, classes PascalCase, and functions or variables snake_case. Follow the existing service/API split: routers should validate and dispatch, while reusable behavior belongs in `services/`. Frontend files use TypeScript/TSX with kebab-case filenames for components and route folders. ESLint is configured in `web/eslint.config.mjs` with Next.js, TypeScript, and Prettier compatibility; note that unused variables and explicit `any` are currently allowed.

## Testing Guidelines

Tests use the standard `unittest` framework. Add new tests as `test/test_<feature>.py`, with test classes ending in `Tests` and methods starting with `test_`. Prefer service-level tests for deterministic logic and API tests for compatibility behavior. Some HTTP tests assume a running backend at `http://localhost:8000` with `Authorization: Bearer chatgpt2api`; document any extra setup in the test file.

## Commit & Pull Request Guidelines

Recent history uses short descriptive subjects, often `feat: ...`, plus merge commits from feature and fix branches. Use concise imperative summaries such as `feat: add image storage stats` or `fix: normalize API error response`. Pull requests should explain the user-facing change, list backend/frontend impact, mention storage or config migrations, link related issues, and include screenshots for UI changes.

## Security & Configuration Tips

Do not commit real account tokens, auth keys, database URLs, or proxy credentials. Start from `.env.example` and override sensitive values through environment variables or local Compose files. When changing `config.json`, keep defaults safe for local development and document any required production setting.
