"""Small NetBox REST helpers for the e2e tests (token, script upload and run, job polling)."""

from __future__ import annotations

import time
from pathlib import Path

import requests

TERMINAL_STATUSES = {"completed", "errored", "failed"}


def provision_token(base_url: str, username: str, password: str) -> tuple[int, str]:
    response = requests.post(
        f"{base_url}/api/users/tokens/provision/",
        json={"username": username, "password": password, "description": "otel-e2e"},
        headers={"Connection": "close"},
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    return body["id"], f"Bearer nbt_{body['key']}.{body['token']}"


def delete_token(base_url: str, header: str, token_id: int) -> None:
    requests.delete(
        f"{base_url}/api/users/tokens/{token_id}/",
        headers={"Authorization": header, "Connection": "close"},
        timeout=10,
    )


def _headers(header: str) -> dict[str, str]:
    return {"Authorization": header, "Accept": "application/json", "Connection": "close"}


def ensure_script(base_url: str, header: str, path: Path, module: str, class_name: str) -> str:
    identifier = f"{module}.{class_name}"
    # This GET only confirms a Script DB record exists: NetBox creates it once, at upload time,
    # via ScriptModule.sync_classes(), and the row persists even if the uploaded file later
    # disappears from storage (for example a scripts volume that got recreated). "vars" is
    # populated from the loaded Python class, so an empty dict here means the file is missing
    # even though the DB record is present; overwrite the module's file in place rather than
    # trusting the 200 status alone.
    found = requests.get(f"{base_url}/api/extras/scripts/{identifier}/", headers=_headers(header), timeout=10)
    if found.status_code == 200:
        body = found.json()
        # Always overwrite the module file with the current dev/scripts/otel_demo.py, even when
        # "vars" shows the existing file already loads fine: otherwise a repo edit to the script
        # never reaches a stack where the Script DB record (and its file) already exist from a
        # previous run.
        with path.open("rb") as fh:
            reupload = requests.patch(
                f"{base_url}/api/extras/scripts/upload/{body['module']}/",
                headers=_headers(header),
                files={"file": (path.name, fh, "text/x-python")},
                timeout=30,
            )
        reupload.raise_for_status()
        return identifier
    with path.open("rb") as fh:
        upload = requests.post(
            f"{base_url}/api/extras/scripts/upload/",
            headers=_headers(header),
            files={"file": (path.name, fh, "text/x-python")},
            timeout=30,
        )
    upload.raise_for_status()
    return identifier


def run_script(base_url: str, header: str, script: str, data: dict) -> int:
    response = requests.post(
        f"{base_url}/api/extras/scripts/{script}/",
        headers=_headers(header),
        json={"data": data, "commit": False},
        timeout=30,
    )
    response.raise_for_status()
    # Verified live against NetBox 4.7.1: the run response is the Script serializer, and the
    # enqueued Job is nested under "result" (top-level "id" is the Script's own id, not the job's).
    return response.json()["result"]["id"]


def wait_for_job(base_url: str, header: str, job_id: int, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = requests.get(f"{base_url}/api/core/jobs/{job_id}/", headers=_headers(header), timeout=10).json()
        if job["status"]["value"] in TERMINAL_STATUSES:
            return job
        time.sleep(1)
    raise TimeoutError(f"job {job_id} did not finish within {timeout}s")
