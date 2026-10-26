"""The credentials file: one env file, mode 600, values never echoed."""

import stat
from pathlib import Path

import pytest

from swarmify_connectors import Credentials


def test_set_writes_a_file_only_its_owner_can_read_and_merges(tmp_path: Path) -> None:
    creds = Credentials(tmp_path / "home" / ".env")
    assert creds.read() == {}
    creds.set({"FAL_KEY": "one"})
    creds.set({"YT_TOKEN": "two", "FAL_KEY": "three"})
    assert stat.S_IMODE(creds.path.stat().st_mode) == 0o600
    assert creds.read() == {"FAL_KEY": "three", "YT_TOKEN": "two"}


def test_read_understands_comments_quotes_and_export(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keys\nexport A_KEY=\"quoted value\"\nB='single'\n\nnot a line\nC = plain\n")
    path.chmod(0o600)
    assert Credentials(path).read() == {"A_KEY": "quoted value", "B": "single", "C": "plain"}


def test_a_file_others_can_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="chmod 600"):
        Credentials(path).read()


@pytest.mark.parametrize("values", [{"lower": "x"}, {"OK": "two\nlines"}])
def test_bad_names_and_line_breaks_are_refused(tmp_path: Path, values: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Credentials(tmp_path / ".env").set(values)
