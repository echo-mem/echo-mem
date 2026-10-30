
import hashlib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from pathlib import Path

try:
    __version__ = _version("echo-mem")
except PackageNotFoundError:  # a source checkout with nothing installed
    __version__ = "0+unknown"


def _source_digest(package_dir: Path | None = None) -> str:
    """A digest of this package's own files as they stand at import.

    A snapshot of the source tree at startup, not an identity of every module
    the process goes on to execute: a module imported after an edit runs the
    new code while this digest keeps naming the tree as it stood at startup.
    Dependencies and configuration are outside its scope.

    importlib.metadata reads the .dist-info the last `pip install` wrote, which
    never moves in an editable checkout - so a version read from it records the
    installer, not the writer. Only the files' names and contents matter here,
    not where they sit: two machines running the same code must read as the
    same writer, or `health` would cry wolf at stores that are fine.
    """
    if package_dir is None:
        package_dir = Path(__file__).parent
    digest = hashlib.sha256()
    files = (p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts)
    for path in sorted(files):
        digest.update(path.relative_to(package_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


# The stamp audit_entry.writer_version carries: the install version for humans,
# plus a digest of this package's source tree as it stood at import. A module
# imported after startup runs whatever the tree holds then; the stamp stays the
# startup snapshot. A server spawned before an upgrade writes its old identity;
# a later health run hashing the tree sees it.
try:
    __writer_version__ = f"{__version__}+{_source_digest()}"
except OSError:  # an unreadable source: say what the column said before
    __writer_version__ = __version__
