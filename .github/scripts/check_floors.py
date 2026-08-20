"""Fail if a floor in requirements.txt sits below the version in uv.lock.

The two files describe the same dependency set for two different audiences:
uv.lock is what docker/Dockerfile.prod installs, requirements.txt is what a
developer following the README installs. They are allowed to differ in *form* —
one is exact, the other is a floor — but a floor below the lock means a fresh
`pip install -r requirements.txt` may resolve to a version nobody has audited.
That is exactly the drift #45 was filed about: the floors still admitted Pillow
10.0.0 (CVE-2023-50447) long after the lock had moved to 12.x.

Packages listed in requirements.txt but absent from the lock are reported and
skipped rather than failed. That is deliberate — a dependency added to
requirements.txt and pyproject.toml before `uv lock` has been run should produce
one clear "run uv lock" failure from this workflow's `uv lock --check` step, not
a second, more confusing one from here.
"""

import re
import sys
import tomllib
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]

# A name, optional extras, then a >= floor. Anything else in requirements.txt —
# an exact ==, a URL, a bare unbounded name — is not a floor and so is not this
# script's business; it is skipped rather than guessed at.
FLOOR = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*>=\s*([^\s,;#]+)")


def locked_versions() -> dict[str, Version]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {
        canonicalize_name(pkg["name"]): Version(pkg["version"])
        for pkg in lock.get("package", [])
        if "version" in pkg
    }


def main() -> int:
    locked = locked_versions()
    failures: list[str] = []
    skipped: list[str] = []

    for line in (ROOT / "requirements.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = FLOOR.match(line)
        if match is None:
            continue

        name, floor = canonicalize_name(match.group(1)), Version(match.group(2))
        if name not in locked:
            skipped.append(f"{name}>={floor}")
        elif floor < locked[name]:
            failures.append(f"  {name}>={floor} is below the locked {locked[name]}")

    if skipped:
        print("Not present in uv.lock, skipped:", ", ".join(sorted(skipped)))

    if failures:
        print("requirements.txt floors are behind uv.lock:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(
            "\nRaise each floor to at least the locked version. The lock is what "
            "the production image installs; the floors decide what a fresh "
            "`pip install -r requirements.txt` is allowed to resolve to.",
            file=sys.stderr,
        )
        return 1

    print("Every requirements.txt floor is at or above its locked version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
