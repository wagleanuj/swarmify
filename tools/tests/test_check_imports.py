from pathlib import Path

import pytest

from check_imports import main, violations


def _package(root: Path, name: str, source: str) -> None:
    pkg = root / name / "src" / f"swarmify_{name}"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(source)


def test_kernel_and_own_package_are_allowed(tmp_path: Path) -> None:
    _package(tmp_path, "kernel", "")
    _package(
        tmp_path,
        "runs",
        "from swarmify_kernel import new_id\n"
        "import swarmify_runs.ports\n"
        "from .ports import Storage\n",
    )
    assert violations(tmp_path) == []


def test_importing_another_context_is_a_violation(tmp_path: Path) -> None:
    _package(tmp_path, "runs", "")
    _package(tmp_path, "swarm", "import swarmify_runs\nfrom swarmify_runs.stages import Stage\n")
    found = violations(tmp_path)
    assert [v.module for v in found] == ["swarmify_runs", "swarmify_runs.stages"]
    assert all(v.package == "swarmify_swarm" for v in found)
    assert found[0].file.name == "__init__.py"


def test_kernel_may_not_import_any_context(tmp_path: Path) -> None:
    _package(tmp_path, "kernel", "from swarmify_runs import Run\n")
    _package(tmp_path, "runs", "")
    assert len(violations(tmp_path)) == 1


def test_main_prints_each_violation_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _package(tmp_path, "swarm", "import swarmify_runs\n")
    assert main(["check_imports", str(tmp_path)]) == 1
    assert "swarmify_swarm imports swarmify_runs" in capsys.readouterr().out
    _package(tmp_path, "runs", "")
    (tmp_path / "swarm/src/swarmify_swarm/__init__.py").write_text("")
    assert main(["check_imports", str(tmp_path)]) == 0
