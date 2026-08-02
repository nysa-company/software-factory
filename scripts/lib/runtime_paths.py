"""Resolve ignored runtime files beside a repository's canonical checkout."""

from pathlib import Path
import subprocess


def canonical_factory_file(root: Path, name: str) -> Path:
    root = root.resolve(strict=True)

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "Git path resolution failed")
        return result.stdout.strip()

    worktree = Path(git("rev-parse", "--show-toplevel")).resolve(strict=True)
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    main = common.resolve(strict=True).parent
    try:
        relative = root.relative_to(worktree)
    except ValueError:
        return root / "factory" / name
    return main / relative / "factory" / name
