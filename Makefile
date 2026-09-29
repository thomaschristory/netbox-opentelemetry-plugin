COMPOSE = docker compose -f dev/docker-compose.yml
# The `ui` profile (grafana/otel-lgtm) with the override that makes the Collector forward to it.
# UI=1 adds it to any dev target, for example `make dev-gunicorn UI=1`.
COMPOSE_UI = -f dev/docker-compose.ui.yml --profile ui
UI_FLAGS = $(if $(filter 1 true yes,$(UI)),$(COMPOSE_UI))

.PHONY: test test-netbox lint format dev dev-gunicorn dev-uwsgi dev-ui down logs-collector e2e dist docs docs-serve

test:
	uv run pytest -W error::DeprecationWarning

test-netbox:
	$(COMPOSE) exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py test --keepdb --noinput --parallel 1 /plugin/tests_netbox

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

dist:
	rm -rf dist
	uv build
	uvx twine check --strict dist/*
	python3 dev/scripts/check_dist.py dist

dev:
	$(COMPOSE) $(UI_FLAGS) up -d --build

dev-gunicorn:
	$(COMPOSE) $(UI_FLAGS) --profile gunicorn up -d --build

dev-uwsgi:
	$(COMPOSE) $(UI_FLAGS) --profile uwsgi up -d --build

dev-ui:
	$(COMPOSE) $(COMPOSE_UI) up -d --build

down:
	$(COMPOSE) $(COMPOSE_UI) --profile gunicorn --profile uwsgi down

logs-collector:
	$(COMPOSE) logs -f otel-collector

e2e:
	uv run pytest -m e2e tests/e2e -v

# Zensical publishes every file under docs_dir and has no exclude setting, so the site is built
# from a staging copy of the files git can see (tracked, or untracked but not ignored). Files
# excluded locally through .gitignore or .git/info/exclude never reach site/.
DOCS_STAGE = build/docs-src

docs:
	rm -rf $(DOCS_STAGE) site
	mkdir -p $(DOCS_STAGE)
	git ls-files -z --cached --others --exclude-standard -- docs mkdocs.yml CHANGELOG.md \
		| tar --null -T - -cf - | tar -xf - -C $(DOCS_STAGE)
	cd $(DOCS_STAGE) && uv run --locked --project $(CURDIR) --group docs zensical build --strict
	mv $(DOCS_STAGE)/site site
	@if find site -path '*superpowers*' | grep -q .; then echo "site/ contains superpowers files"; exit 1; fi

# Serves the working tree directly, including locally excluded files; for local preview only.
docs-serve:
	uv run --group docs zensical serve
