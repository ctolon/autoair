.PHONY: dev-airflow serve ui-dev codegen check audit test matrix e2e crawl build docs docs-serve docs-capture \
        demo demo-down image

dev-airflow:      ## Local Airflow 3 with the plugin installed (http://localhost:8080)
	./scripts/dev-airflow.sh

serve:            ## Standalone builder (http://127.0.0.1:8765/ui/)
	uv run autoair serve

ui-dev:           ## Vite dev server proxying /api to `make serve`
	cd frontend && npm run dev

codegen:          ## Regenerate the TypeScript client from the backend OpenAPI
	cd frontend && npm run codegen

check:            ## All static checks (the same as CI's lint jobs)
	uv run ruff check src tests deploy && uv run ruff format --check src tests deploy && uv run mypy
	uv run python scripts/scan_hardcoded.py --check
	uv run python scripts/gen_config_docs.py --check
	uv run python scripts/gen_shared_constants.py --check
	cd frontend && npm run typecheck && npm run lint && npm run format:check

audit:            ## Hard-coded values report in .artifacts/hardcoded-audit.md (gitignored, not committed)
	uv run python scripts/scan_hardcoded.py --write

test:             ## Unit + integration tests (current Airflow) and frontend tests
	uv run pytest -q
	cd frontend && npm test

matrix:           ## Python tests against every supported Airflow minor
	uv run nox

e2e:              ## Browser test against a running `make dev-airflow`
	cd frontend && npx playwright test --grep-invert crawl

crawl:            ## UI crawler: every menu/dialog/tab/inspector must render without errors (~25 min)
	cd frontend && npx playwright test e2e/crawl.spec.ts --workers 6

build:            ## Frontend bundles + wheel
	cd frontend && npm ci && npm run build
	uv build

docs:             ## Documentation website (website/build); syncs the generated configuration reference first
	cd website && npm ci && npm run build

docs-serve:       ## Documentation website with live reload (http://localhost:3000/autoair/)
	cd website && npm ci && npm start

docs-capture:     ## Re-capture the website's screenshots and videos (needs Airflow on :8080)
	cd website && npm run capture

image: build      ## Container image (Airflow + AutoAir) from dist/: autoair:local
	docker build -f deploy/docker/Dockerfile -t autoair:local .

DEMO = docker compose -f deploy/compose/docker-compose.demo.yml

demo:             ## Docker Compose demo stack (deploy/compose/README.md); PROFILES="lb git" ENV="env/git.env"
	$(DEMO) $(if $(ENV),--env-file deploy/compose/.env) $(foreach e,$(ENV),--env-file deploy/compose/$(e)) \
		$(foreach p,$(PROFILES),--profile $(p)) up --build -d --wait

demo-down:        ## Stop the demo stack (keeps volumes; `$(DEMO) down -v` erases them)
	$(DEMO) $(foreach p,$(PROFILES),--profile $(p)) down
