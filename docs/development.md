# Development

This page covers the repository layout, the local development stack, the tests, the documentation build, the CI workflows and the release procedure.

## Repository layout

| Path | Content |
| --- | --- |
| `netbox_opentelemetry_plugin/` | The plugin package. All OpenTelemetry SDK imports live in `otel.py`. |
| `tests/` | Unit tests (pytest). `tests/e2e/` holds the end-to-end tests that run against the dev stack. |
| `tests_netbox/` | Integration tests that run inside NetBox with `manage.py test`. |
| `dev/` | Docker compose dev stack: `Dockerfile`, `docker-compose.yml`, NetBox plugin configuration, Collector configuration, environment files and helper scripts. |
| `docs/` | This documentation, built with Zensical from `mkdocs.yml`. |
| `.github/workflows/` | CI, release and documentation workflows. |

Requirements: Python 3.12 or later, [uv](https://docs.astral.sh/uv/), Docker with the compose plugin, and `make`.

## Dev stack

The stack is based on the netbox-docker image, with the plugin installed in editable mode and its source bind-mounted into the containers. Code changes apply after a container restart.

| Command | Starts |
| --- | --- |
| `make dev` | NetBox on Granian, the RQ worker, Postgres, two Valkey instances, the Collector and a webhook sink. |
| `make dev-gunicorn` | The same, plus NetBox on gunicorn with `--preload`. |
| `make dev-uwsgi` | The same, plus NetBox on uWSGI (pyuwsgi, master mode) behind nginx, laid out like NetBox's `contrib/uwsgi.ini`: uWSGI listens on a uwsgi-protocol socket and nginx forwards to it with `uwsgi_pass`. |
| `make dev-ui` | The same as `make dev`, plus Grafana with Loki, Tempo and Prometheus (profile `ui`, see [Grafana UI](#grafana-ui)). |
| `make down` | Stops every service of all profiles. |

To run all three web servers at once, as the scheduled CI job does:

```bash
docker compose -f dev/docker-compose.yml --profile gunicorn --profile uwsgi up -d --build --wait --wait-timeout 600
```

Ports on the host:

| Port | Service |
| --- | --- |
| 8000 | NetBox on Granian |
| 8001 | NetBox on gunicorn (profile `gunicorn`) |
| 8002 | NetBox on uWSGI, through nginx (profile `uwsgi`) |
| 3000 | Grafana (profile `ui`) |
| 4317 | Collector, OTLP gRPC |
| 4318 | Collector, OTLP HTTP |

The NetBox superuser is `admin` with password `admin`.

The Collector writes everything it receives to `dev/data/collector/` (`logs.json`, `traces.json`, `metrics.json`, one JSON document per line) and prints it with the `debug` exporter. `make logs-collector` follows the Collector's output. The files grow without limit; delete them while the stack is stopped if they get too large.

### Grafana UI

The `ui` profile adds [grafana/otel-lgtm](https://github.com/grafana/docker-otel-lgtm), one container with Grafana, Loki (logs), Tempo (traces) and Prometheus (metrics). The Collector then sends everything it receives to it over OTLP gRPC, in addition to the `debug` and `file` exporters, so the files in `dev/data/collector/` and the e2e tests work the same with or without it.

```bash
make dev-ui                 # Granian, as make dev
make dev-gunicorn UI=1      # any dev target takes UI=1
make dev-uwsgi UI=1
```

Grafana is on `http://localhost:3000` with anonymous access and the Admin role, so no login is needed. The `admin` user has the password `admin`. The data sources `Loki`, `Tempo` and `Prometheus` are provisioned by the image. NetBox telemetry has the service name `netbox`: in Explore, query Loki with `{service_name="netbox"}`, search Tempo for the service `netbox`, and query Prometheus for metrics such as `http_server_request_duration_seconds_count`. The image keeps its data inside the container, so it is lost when the container is recreated.

Selecting a Collector configuration is not something a compose profile can do, so the profile comes with an override file. `make dev-ui` runs:

```bash
docker compose -f dev/docker-compose.yml -f dev/docker-compose.ui.yml --profile ui up -d --build
```

`dev/docker-compose.ui.yml` starts the Collector with a second `--config` flag that loads `dev/otelcol/ui.yaml` over `dev/otelcol/config.yaml`. That overlay adds the `otlp_grpc/lgtm` exporter to the three pipelines. Without the override the Collector has no exporter pointing at otel-lgtm, so the default stack does not depend on it and logs no export errors when it is absent. Using the override without `--profile ui` fails at once with `service "otel-collector" depends on undefined service "otel-lgtm"`. With `--profile ui` alone, Grafana starts but receives nothing.

Running `make dev` after `make dev-ui` recreates the Collector with the default configuration and leaves otel-lgtm running; `make down` stops it.

The Grafana, Loki, Tempo and Prometheus APIs can be queried through Grafana's data source proxy, without publishing more ports:

```bash
curl -s 'http://localhost:3000/api/datasources/proxy/uid/prometheus/api/v1/query' \
  --data-urlencode 'query=count by (__name__) ({service_name="netbox"})'
curl -s 'http://localhost:3000/api/datasources/proxy/uid/loki/loki/api/v1/query_range' \
  --data-urlencode 'query={service_name="netbox"}' --data-urlencode 'limit=5'
curl -s 'http://localhost:3000/api/datasources/proxy/uid/tempo/api/search?tags=service.name%3Dnetbox&limit=5'
```

## Tests

| Command | What it runs |
| --- | --- |
| `make test` | Unit tests, with `DeprecationWarning` turned into errors. No stack needed. |
| `make test-netbox` | Integration tests in `tests_netbox/`, inside the running `netbox` container. |
| `make e2e` | End-to-end tests in `tests/e2e/` against the running stack. The worker test covers each web server that is running and skips the ones that refuse the connection; a server that answers `/login/` with anything but 200 fails the test. |
| `make lint` | `ruff check` and `ruff format --check`. |
| `make format` | Applies ruff fixes and formatting. |

The unit tests run on Python 3.12, 3.13 and 3.14 in CI. To run them locally with a given version:

```bash
UV_PYTHON=3.13 uv run pytest
```

## Documentation

| Command | What it does |
| --- | --- |
| `make docs` | Builds the site into `site/` with `zensical build --strict`. Zensical publishes every file under `docs/`, so the build runs on a staging copy (`build/docs-src/`) of the files git can see: files excluded through `.gitignore` or `.git/info/exclude` are never published. The target also fails if the output contains such files. |
| `make docs-serve` | Serves the working tree with live reload on `http://localhost:8000`, for local preview only (it includes locally excluded files). Stop the dev stack first, or pass another address with `uv run --group docs zensical serve -a 127.0.0.1:8010`. |

The documentation tools are in the `docs` dependency group of `pyproject.toml`; `uv run --group docs` installs them on first use.

## CI

Three workflows live in `.github/workflows/`.

`ci.yml` runs on pushes to `main`, on pull requests, every night at 03:17 UTC and on manual dispatch.

| Job | What it checks |
| --- | --- |
| `lint` | ruff check and format check. |
| `unit` | Unit tests on Python 3.12, 3.13 and 3.14, with `uv sync --locked` so a stale `uv.lock` fails. |
| `docs` | `make docs` (`zensical build --strict`) with the locked documentation dependencies. |
| `netbox-load` | Installs NetBox from source with the plugin on Python 3.12, runs `manage.py check`, checks that the log handler is attached, and checks that a configuration without any endpoint starts and prints `no endpoint configured`. |
| `netbox-integration` | Runs `tests_netbox/` with `manage.py test` against Postgres and Valkey service containers. |
| `e2e` | Nightly and on manual dispatch only. Starts the dev stack with all three web servers and runs `tests/e2e/`. On failure, the compose logs and the Collector output are uploaded as the `e2e-logs` artifact. |

GitHub disables scheduled workflows in a public repository after 60 days without repository activity. The nightly run then stops until the workflow is enabled again from the Actions tab, or with `gh workflow enable ci.yml`.

`release.yml` runs on tags matching `v*` and on manual dispatch. It builds the sdist and the wheel, runs `twine check --strict` and `dev/scripts/check_dist.py` (archive contents, and for a tag, that the tag equals `v` followed by the package version). For a tag it also checks that the tagged commit is on `main`. A tag push publishes to PyPI; a manual run publishes to TestPyPI only, whatever branch or tag it is started on. Both use trusted publishing (OIDC), so no API token is stored in the repository, and attestations are generated by default. `ci.yml` does not run on tags and `release.yml` does not run the tests, so a release relies on CI having passed on the tagged commit on `main`.

`docs.yml` runs on tags matching `v*` and on manual dispatch. It builds the documentation and deploys it to GitHub Pages. It is independent of `release.yml`: on a tag, the documentation can be deployed even if publishing to PyPI fails, and the other way round.

## Updating OpenTelemetry

The OpenTelemetry packages are pinned in `pyproject.toml` (`~=` for the API, SDK and exporters, `==` for the instrumentations). The fork handling depends on how the SDK behaves in a forked child (see [How it works](how-it-works.md)), and two unit tests reach into private SDK attributes. On every bump of the SDK minor version:

1. Update the pins and `uv.lock` (`uv lock`), then run `make test`.
2. `test_sdk_internals_used_by_the_fork_lock_test_were_reviewed_for_this_sdk` in `tests/test_fork.py` fails until the next two steps are done. Its message names the SDK attribute paths in question.
3. Check that `test_fork_while_batch_worker_lock_is_held` still runs rather than skips (`uv run pytest tests/test_fork.py -rs`). It holds the lock the SDK's log batch worker waits on, found through `_batch_worker_condition`. If the SDK moved that lock, update `_batch_worker_condition` and `BATCH_WORKER_CONDITION_PATH`. Check the same for `test_grpc_exporters_are_rebuilt_in_forked_children_and_deliver`, which reads the queues of the batch processors a forked child inherits, found through `_batch_queues`. If the SDK moved them, update `_batch_queues` and `BATCH_QUEUE_PATHS`.
4. Set `REVIEWED_OTEL_SDK` in `tests/test_fork.py` to the new minor version.
5. Check that the statements about the SDK in section 4.2 of `SPEC.md` still hold: the SDK re-initialises its batch processors in a forked child and clears their queues, keeps the parent's Resource and shares the parent's exporter. The fork tests in `tests/test_fork.py`, including the one with a real gRPC receiver, cover the plugin side of this.

## Releasing

1. Set the new version in `netbox_opentelemetry_plugin/version.py`. In `CHANGELOG.md`, move the entries from `[Unreleased]` under a new heading `## [<version>] - YYYY-MM-DD` and update the link references at the bottom of the file. If the supported NetBox range changes, update the compatibility table in `README.md` and in [the docs home page](index.md#compatibility). Commit on `main`.

2. Check that everything passes locally:

    ```bash
    make test lint docs dist
    ```

3. First release only:

    - Create the GitHub repository `thomaschristory/netbox-opentelemetry-plugin` and push the `main` branch.
    - In the repository settings, under Pages, set the source to "GitHub Actions".
    - Create the environments `pypi`, `testpypi` and `github-pages`, optionally with required reviewers.
    - In the `github-pages` environment, under "Deployment branches and tags", add a tag rule `v*`. With the Pages source set to "GitHub Actions", GitHub restricts this environment to the default branch, and a deployment from a tag is rejected without that rule. If you restrict the `pypi` environment to selected branches and tags, add the same `v*` tag rule there.
    - On PyPI and on TestPyPI, under Account settings, Publishing, register a pending trusted publisher with project name `netbox-opentelemetry-plugin`, owner `thomaschristory`, repository `netbox-opentelemetry-plugin`, workflow `release.yml`, and environment `pypi` (on PyPI) or `testpypi` (on TestPyPI).

4. Push `main` and wait for CI to pass on the release commit:

    ```bash
    git push origin main
    gh run list --workflow ci.yml --branch main --commit "$(git rev-parse HEAD)" --limit 1
    gh run watch <run-id> --exit-status
    ```

    Optionally, run the full CI including the `e2e` job on `main` and wait for it the same way:

    ```bash
    gh workflow run ci.yml --ref main
    ```

    Only tag a commit that is on `main` and has a green CI run. The release workflow refuses a tag whose commit is not on `main`.

5. Optional dry run: run the `release` workflow manually on `main` (`gh workflow run release.yml --ref main`, or from the Actions tab). It publishes to TestPyPI only. TestPyPI refuses a version it already has, so a second dry run needs a new version number. Then install the package into a scratch virtual environment:

    ```bash
    uv venv /tmp/release-check
    uv pip install --python /tmp/release-check/bin/python \
      --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
      netbox-opentelemetry-plugin==<version>
    ```

6. Tag the release commit on `main` and push the tag:

    ```bash
    git tag -a v<version> -m "v<version>"
    git push origin v<version>
    ```

    The tag starts `release.yml`, which checks, builds, and publishes to PyPI with attestations, and `docs.yml`, which deploys the documentation.

7. Create the GitHub release with the CHANGELOG section of the version as notes. Copy that section (without its heading) into a file, then:

    ```bash
    gh release create v<version> --title v<version> --notes-file <notes-file>
    ```
