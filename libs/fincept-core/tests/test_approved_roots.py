"""Tests for ``fincept_core.datasets.approved_roots``.

Covers the fail-closed gate end-to-end: in-root relative and absolute
paths, traversal (``..``) in leading / middle / whole-path positions,
symlink escape, the env-var default factory, dev extra roots, and the
``ApprovedRootsError`` shape (``ValueError`` subclass + ``code``).
"""

from __future__ import annotations

import contextlib
import os
import pathlib

import pytest

from fincept_core.datasets.approved_roots import (
    ApprovedRoots,
    ApprovedRootsError,
    ResolvedPath,
    default_approved_roots,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def data_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "data"
    root.mkdir()
    (root / "captures").mkdir()
    (root / "captures" / "btc.parquet").write_bytes(b"")
    return root


@pytest.fixture
def gate(data_root: pathlib.Path) -> ApprovedRoots:
    return ApprovedRoots(roots=[data_root])


def _can_symlink() -> bool:
    """True if the current process can create a symlink."""
    probe = pathlib.Path(os.path.join(os.getcwd(), "_aprobed_symlink"))
    target = pathlib.Path(os.path.join(os.getcwd(), "_aprobed_target"))
    try:
        target.write_bytes(b"")
        os.symlink(target, probe)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        for p in (probe, target):
            with contextlib.suppress(OSError):
                p.unlink()


# --------------------------------------------------------------------------- #
# Happy paths                                                                 #
# --------------------------------------------------------------------------- #


def test_relative_path_inside_data_root(
    data_root: pathlib.Path,
    gate: ApprovedRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative path under an approved root resolves cleanly."""
    monkeypatch.chdir(data_root.parent)
    rp = gate.resolve("data/captures/btc.parquet")
    assert isinstance(rp, ResolvedPath)
    assert rp.inside_root == data_root.resolve(strict=False)
    assert rp.path == (data_root / "captures" / "btc.parquet").resolve(
        strict=False
    )


def test_absolute_path_inside_data_root(
    data_root: pathlib.Path, gate: ApprovedRoots
) -> None:
    """An absolute path under an approved root resolves cleanly."""
    candidate = data_root / "captures" / "btc.parquet"
    rp = gate.resolve(str(candidate))
    assert rp.inside_root == data_root.resolve(strict=False)
    assert rp.path == candidate.resolve(strict=False)


def test_dev_extra_root_is_allowed(tmp_path: pathlib.Path) -> None:
    """``extra_dev_roots`` admits a scratch dir without polluting prod."""
    prod = tmp_path / "data"
    prod.mkdir()
    dev = tmp_path / "scratch"
    dev.mkdir()
    (dev / "fixture.parquet").write_bytes(b"")
    gate = ApprovedRoots(roots=[prod], extra_dev_roots=[dev])
    rp = gate.resolve(str(dev / "fixture.parquet"))
    assert rp.inside_root == dev.resolve(strict=False)


# --------------------------------------------------------------------------- #
# Failure paths                                                               #
# --------------------------------------------------------------------------- #


def test_traversal_leading_dotdot_rejected(
    data_root: pathlib.Path,
    gate: ApprovedRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``data/../etc`` is rejected with ``code=traversal``."""
    monkeypatch.chdir(data_root.parent)
    with pytest.raises(ApprovedRootsError) as exc:
        gate.resolve("data/../etc/passwd")
    assert exc.value.code == "traversal"


def test_traversal_middle_dotdot_rejected(
    data_root: pathlib.Path,
    gate: ApprovedRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``data/foo/../etc`` is rejected with ``code=traversal``."""
    monkeypatch.chdir(data_root.parent)
    with pytest.raises(ApprovedRootsError) as exc:
        gate.resolve("data/foo/../etc/passwd")
    assert exc.value.code == "traversal"


def test_dotdot_as_whole_path_rejected(
    gate: ApprovedRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``..`` alone is rejected with ``code=traversal``."""
    monkeypatch.chdir(pathlib.Path.cwd())
    with pytest.raises(ApprovedRootsError) as exc:
        gate.resolve("..")
    assert exc.value.code == "traversal"


def test_absolute_path_outside_root_rejected(
    gate: ApprovedRoots, tmp_path: pathlib.Path
) -> None:
    """``/etc/passwd`` (or a tmp-path analog) is rejected as outside."""
    outside = tmp_path / "etc" / "passwd"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"")
    with pytest.raises(ApprovedRootsError) as exc:
        gate.resolve(str(outside))
    assert exc.value.code == "outside_root"


def test_symlink_escape_rejected(
    data_root: pathlib.Path, gate: ApprovedRoots, tmp_path: pathlib.Path
) -> None:
    """A symlink pointing outside the root is rejected when disallowed."""
    if not _can_symlink():
        pytest.skip("symlink creation not supported on this platform")
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"sensitive")
    link = data_root / "leak.parquet"
    os.symlink(outside, link)
    with pytest.raises(ApprovedRootsError) as exc:
        gate.resolve(str(link))
    # An escaping symlink is caught by the root check after resolve().
    assert exc.value.code in {"outside_root", "symlink_escape"}


def test_symlink_allowed_when_flag_set(
    data_root: pathlib.Path, gate: ApprovedRoots, tmp_path: pathlib.Path
) -> None:
    """``allow_symlinks=True`` admits an in-root symlink."""
    if not _can_symlink():
        pytest.skip("symlink creation not supported on this platform")
    target = data_root / "captures" / "btc.parquet"
    link = data_root / "alias.parquet"
    os.symlink(target, link)
    rp = gate.resolve(str(link), allow_symlinks=True)
    assert rp.inside_root == data_root.resolve(strict=False)


# --------------------------------------------------------------------------- #
# Default factory + error shape                                               #
# --------------------------------------------------------------------------- #


def test_default_approved_roots_missing_env_var(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing env var falls back to the fail-closed ``["data","models"]``."""
    monkeypatch.delenv("FINCEPT_APPROVED_DATA_ROOTS", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "models").mkdir()
    ar = default_approved_roots()
    resolved = {r.resolve(strict=False) for r in ar.roots}
    assert {(tmp_path / "data").resolve(), (tmp_path / "models").resolve()} <= resolved


def test_default_approved_roots_env_var_override(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit env var overrides the default root list."""
    custom = tmp_path / "datasets"
    custom.mkdir()
    monkeypatch.setenv("FINCEPT_APPROVED_DATA_ROOTS", str(custom))
    ar = default_approved_roots()
    assert ar.roots == (custom.resolve(strict=False),)


def test_error_is_value_error_subclass() -> None:
    """``ApprovedRootsError`` is catchable as ``ValueError``."""
    ar = ApprovedRoots(roots=[pathlib.Path("data")])
    with pytest.raises(ValueError):
        ar.resolve("/etc/passwd")


def test_empty_roots_rejected() -> None:
    """Constructing with no roots at all is fail-closed."""
    with pytest.raises(ApprovedRootsError) as exc:
        ApprovedRoots(roots=[])
    assert exc.value.code == "no_roots"
