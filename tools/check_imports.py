"""Import-boundary check: a bounded context imports the shared kernel and itself, nothing else.

Usage: python tools/check_imports.py packages
Each `packages/<name>/src/swarmify_<name>` package is scanned; any `swarmify_*` import that is
not `swarmify_kernel` or the package's own name fails the check. The api app is the composition
root and lives outside `packages`, so it is not scanned.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

KERNEL = "swarmify_kernel"


@dataclass(frozen=True)
class Violation:
    package: str
    file: Path
    line: int
    module: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.package} imports {self.module}"


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def violations(packages_dir: Path) -> list[Violation]:
    found: list[Violation] = []
    for pkg_dir in sorted(packages_dir.glob("*/src/swarmify_*")):
        package = pkg_dir.name
        for file in sorted(pkg_dir.rglob("*.py")):
            tree = ast.parse(file.read_text(), filename=str(file))
            for line, module in _imported_modules(tree):
                top = module.split(".")[0]
                if top.startswith("swarmify_") and top not in (KERNEL, package):
                    found.append(Violation(package, file, line, module))
    return found


def main(argv: list[str]) -> int:
    found = violations(Path(argv[1]) if len(argv) > 1 else Path("packages"))
    for v in found:
        print(v)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
