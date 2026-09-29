"""dev/scripts/check_dist.py: the release check on the built sdist and wheel."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "dev" / "scripts" / "check_dist.py"
_spec = importlib.util.spec_from_file_location("check_dist", SCRIPT)
check_dist = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_dist)

NAME = check_dist.NAME
METADATA = (
    "Metadata-Version: 2.4\nLicense-Expression: Apache-2.0\nLicense-File: LICENSE\n\nSee [docs](https://example.com).\n"
)


def _sdist(dist: Path, version: str, name: str | None = None) -> None:
    path = dist / (name or f"{NAME}-{version}.tar.gz")
    with tarfile.open(path, "w:gz") as archive:
        for inner in ("PKG-INFO", f"{NAME}/__init__.py"):
            data = b"x"
            info = tarfile.TarInfo(f"{NAME}-{version}/{inner}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _wheel(dist: Path, filename: str, version: str, metadata: str | None = METADATA) -> None:
    info = f"{NAME}-{version}.dist-info"
    with zipfile.ZipFile(dist / filename, "w") as archive:
        archive.writestr(f"{NAME}/__init__.py", "")
        archive.writestr(f"{info}/licenses/LICENSE", "")
        if metadata is not None:
            archive.writestr(f"{info}/METADATA", metadata)


def test_valid_distributions_pass(tmp_path, capsys):
    _sdist(tmp_path, "1.2.3")
    _wheel(tmp_path, f"{NAME}-1.2.3-py3-none-any.whl", "1.2.3")
    assert check_dist.main([str(tmp_path), "--tag", "v1.2.3"]) == 0
    assert capsys.readouterr().out.startswith("ok: ")


@pytest.mark.parametrize(
    "filename",
    [
        f"{NAME}.whl",
        f"{NAME}-1.2.3.whl",
        f"{NAME}-1.2.3-py3-none.whl",
        f"{NAME}-1.2.3-x-py3-none-any.whl",
        f"{NAME}-1.2.3-1-extra-py3-none-any.whl",
        "other_package-1.2.3-py3-none-any.whl",
    ],
)
def test_malformed_wheel_filename_is_a_one_line_error(tmp_path, capsys, filename):
    _sdist(tmp_path, "1.2.3")
    (tmp_path / filename).write_bytes(b"")
    assert check_dist.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].startswith(f"malformed wheel filename: {filename} ")


def test_wheel_filename_with_build_tag_is_accepted():
    assert check_dist.wheel_version(f"{NAME}-1.2.3-1-py3-none-any.whl") == "1.2.3"


def test_corrupt_archives_are_reported(tmp_path, capsys):
    (tmp_path / f"{NAME}-1.2.3.tar.gz").write_bytes(b"not a tarball")
    (tmp_path / f"{NAME}-1.2.3-py3-none-any.whl").write_bytes(b"not a zip")
    assert check_dist.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert all("cannot read the archive" in line for line in out)


def test_missing_metadata_is_reported(tmp_path, capsys):
    _sdist(tmp_path, "1.2.3")
    _wheel(tmp_path, f"{NAME}-1.2.3-py3-none-any.whl", "1.2.3", metadata=None)
    assert check_dist.main([str(tmp_path)]) == 1
    assert "METADATA missing from dist-info" in capsys.readouterr().out


def test_sdist_and_wheel_version_mismatch_is_reported(tmp_path, capsys):
    _sdist(tmp_path, "1.2.4")
    _wheel(tmp_path, f"{NAME}-1.2.3-py3-none-any.whl", "1.2.3")
    assert check_dist.main([str(tmp_path)]) == 1
    assert "sdist and wheel versions differ" in capsys.readouterr().out
