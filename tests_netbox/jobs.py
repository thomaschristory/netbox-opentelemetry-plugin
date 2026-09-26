"""Job functions for the traces and metrics integration tests, imported by rq by dotted path."""

import requests


def fetch(url):
    return requests.get(url, timeout=2).status_code


def noop(**kwargs):
    return None


def boom(**kwargs):
    raise RuntimeError("integration test failure")
