"""Checks on the user-facing docs: style, runnable config snippets, and OTEL_* names."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from netbox_opentelemetry_plugin import conf

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ENDPOINT_ENV = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}

# Every OTEL_* name the docs may mention: read by conf.py, or by the OpenTelemetry SDK itself.
_SIGNALS = ("LOGS", "TRACES", "METRICS")
_SUFFIXES = ("ENDPOINT", "PROTOCOL", "HEADERS", "TIMEOUT", "INSECURE", "CERTIFICATE")
KNOWN_OTEL_NAMES = frozenset(
    {f"OTEL_EXPORTER_OTLP_{suffix}" for suffix in _SUFFIXES}
    | {f"OTEL_EXPORTER_OTLP_{signal}_{suffix}" for signal in _SIGNALS for suffix in _SUFFIXES}
    | {
        "OTEL_SDK_DISABLED",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_TRACES_SAMPLER",
        "OTEL_TRACES_SAMPLER_ARG",
        "OTEL_METRIC_EXPORT_INTERVAL",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
        "OTEL_SEMCONV_STABILITY_OPT_IN",
    }
)


def doc_pages() -> list[Path]:
    pages = [p for p in DOCS.rglob("*.md") if "superpowers" not in p.relative_to(DOCS).parts]
    return sorted(pages) + [ROOT / "README.md", ROOT / "CHANGELOG.md"]


def python_blocks(text: str) -> list[str]:
    return re.findall(r"^```python\n(.*?)^```", text, flags=re.S | re.M)


def _config_snippets():
    for page in doc_pages():
        for index, block in enumerate(python_blocks(page.read_text())):
            if "PLUGINS_CONFIG" in block:
                yield pytest.param(block, id=f"{page.relative_to(ROOT)}#{index}")


def test_docs_exist():
    assert (DOCS / "index.md").exists()


@pytest.mark.parametrize("page", doc_pages(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_em_dash(page):
    assert "—" not in page.read_text()


@pytest.mark.parametrize("block", list(_config_snippets()))
def test_python_snippets_resolve_without_warnings(block):
    namespace: dict = {}
    exec(compile(block, "<snippet>", "exec"), namespace)
    user = namespace["PLUGINS_CONFIG"]["netbox_opentelemetry_plugin"]
    settings = conf.resolve(user, ENDPOINT_ENV)
    assert settings.enabled
    assert settings.warnings == ()


def test_otel_names_are_read():
    unknown = {}
    for page in doc_pages():
        for name in set(re.findall(r"\bOTEL_[A-Z_]+[A-Z]\b", page.read_text())):
            if name not in KNOWN_OTEL_NAMES:
                unknown.setdefault(name, []).append(str(page.relative_to(ROOT)))
    assert unknown == {}


def test_mkdocs_config_excludes_superpowers():
    text = (ROOT / "mkdocs.yml").read_text()
    assert re.search(r"^exclude_docs: \|\n\s+superpowers/", text, flags=re.M)


ROW = re.compile(r"^\| `([a-z_]+(?:\.[a-z_]+)*)` \| `([^`]*)` \|", flags=re.M)


def _flatten(defaults: dict, prefix: str = ""):
    for key, value in defaults.items():
        if isinstance(value, dict) and value:
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


def test_configuration_reference_matches_defaults():
    text = (DOCS / "configuration.md").read_text()
    rows = ROW.findall(text)
    documented = {key: ast.literal_eval(default) for key, default in rows}
    assert len(rows) == len(documented)
    assert documented == dict(_flatten(conf.DEFAULTS))
