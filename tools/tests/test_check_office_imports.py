from pathlib import Path

from check_office_imports import main, violations


def _file(src: Path, path: str, source: str) -> None:
    (src / path).parent.mkdir(parents=True, exist_ok=True)
    (src / path).write_text(source)


def test_the_office_may_import_packages_itself_the_api_and_the_skin(tmp_path: Path) -> None:
    _file(
        tmp_path,
        "office/Office.tsx",
        'import { Link } from "@tanstack/react-router";\n'
        'import { api } from "../api/client";\n'
        'import type { Run } from "../api/runs";\n'
        'import { role } from "../skin";\n'
        'import { Room } from "./Room";\n'
        'import "./office.css";\n',
    )
    _file(tmp_path, "router.tsx", 'const Office = () => import("./office/Office");\n')
    assert violations(tmp_path) == []
    assert main(["check", str(tmp_path)]) == 0


def test_a_plain_page_or_its_styles_in_the_office_is_a_violation(tmp_path: Path) -> None:
    _file(
        tmp_path,
        "office/Office.tsx",
        'import { figures } from "../pages/WorldOverview";\n'
        'import "../styles/overview.css";\n'
        'const Shell = await import("../Shell");\n',
    )
    found = violations(tmp_path)
    assert [f.split(": ", 1)[1] for f in found] == [
        "the office imports ../pages/WorldOverview",
        "the office imports ../styles/overview.css",
        "the office imports ../Shell",
    ]
    assert found[1].endswith("Office.tsx:2: the office imports ../styles/overview.css")
    assert main(["check", str(tmp_path)]) == 1


def test_the_app_loads_the_office_lazily_only(tmp_path: Path) -> None:
    _file(tmp_path, "office/Office.tsx", "")
    _file(tmp_path, "router.tsx", 'import { Office } from "./office/Office";\n')
    _file(tmp_path, "office/Office.test.tsx", 'import { mountAt } from "../test/mount";\n')
    assert [f.split(": ", 1)[1] for f in violations(tmp_path)] == [
        "./office/Office is imported statically; load it with import()"
    ]


def test_an_office_stylesheet_importing_plain_styles_is_a_violation(tmp_path: Path) -> None:
    _file(tmp_path, "office/office.css", '@import "./room.css";\n@import url("../index.css");\n')
    found = [f.split(": ", 1)[1] for f in violations(tmp_path)]
    assert found == ["the office imports ../index.css"]
