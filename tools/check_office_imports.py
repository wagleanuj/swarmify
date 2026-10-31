"""Import-boundary check for the web app's Office skin.

Usage: python tools/check_office_imports.py apps/web/src
The office (`src/office`) may import packages, itself, the API layer (`src/api`) and the skin
module (`src/skin.ts`), never a plain page or its styles. The rest of the app may load the
office only with a dynamic `import()`, so its code loads when an office route opens. Tests are
not scanned: they mount the whole app.
"""

import re
import sys
from pathlib import Path

STATIC = re.compile(r"""(?:\bfrom|^\s*import)\s*["']([^"']+)["']""", re.MULTILINE)
DYNAMIC = re.compile(r"""\bimport\(\s*["']([^"']+)["']""")


def violations(src: Path) -> list[str]:
    src = src.resolve()
    office = src / "office"
    found: list[str] = []
    for file in sorted(src.rglob("*.ts*")):
        if ".test." in file.name:
            continue
        text = file.read_text()
        inside = file.is_relative_to(office)
        matches = [*STATIC.finditer(text), *(DYNAMIC.finditer(text) if inside else [])]
        for match in matches:
            spec = match.group(1)
            if not spec.startswith("."):
                continue  # a package
            target = (file.parent / spec).resolve()
            where = f"{file}:{text.count(chr(10), 0, match.start()) + 1}"
            if inside:
                ok = target.is_relative_to(office) or target.is_relative_to(src / "api")
                if not ok and target.with_suffix("") != src / "skin":
                    found.append(f"{where}: the office imports {spec}")
            elif target.is_relative_to(office):
                found.append(f"{where}: {spec} is imported statically; load it with import()")
    return found


def main(argv: list[str]) -> int:
    found = violations(Path(argv[1]) if len(argv) > 1 else Path("apps/web/src"))
    for v in found:
        print(v)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
