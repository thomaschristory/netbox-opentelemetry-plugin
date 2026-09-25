"""Order the multi-worker check before the single-login check.

Both tests log in to the Granian service (port 8000) and both use a 5 s lookback window to
tolerate host/VM clock skew. If the single-login test ran first, its login can still be inside
that window when the multi-worker test starts moments later, inflating its exact login count by
one. Running the multi-worker test first avoids the overlap: nothing has logged in to Granian
yet when it starts.
"""


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item: 0 if "test_workers_e2e" in item.nodeid else 1)
