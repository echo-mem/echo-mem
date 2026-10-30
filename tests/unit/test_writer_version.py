"""The version stamped on writes names the code that ran, not the install.

importlib.metadata reads the .dist-info the last `pip install` wrote, which
never moves in an editable checkout - the column recorded the installer, not
the writer, and the health check compared the installer against itself.
"""

from importlib import import_module

from echo_memory import _source_digest


def test_the_digest_answers_which_code_not_which_checkout(tmp_path):
    """Two trees holding the same files hash alike, wherever they sit: two
    machines running the same code read as the same writer. A recompile is
    not a change of code."""
    paths = []
    for name in ("a", "b"):
        pkg = tmp_path / name / "echo_memory"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("x = 1\n")
        (pkg / "write_episode.py").write_text("y = 2\n")
        if name == "a":
            pycache = pkg / "__pycache__"
            pycache.mkdir()
            (pycache / "write_episode.cpython-311.pyc").write_bytes(b"\x00")
        paths.append(pkg)

    assert _source_digest(paths[0]) == _source_digest(paths[1])


def test_an_edit_moves_the_digest(tmp_path):
    """The state every development machine lives in - metadata frozen at some
    past install, source edited since - is only detectable if the identity
    moves when the code does."""
    pkg = tmp_path / "echo_memory"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    before = _source_digest(pkg)

    (pkg / "write_episode.py").write_text("y = 2\n")

    assert _source_digest(pkg) != before


def test_a_late_import_after_an_edit_leaves_the_stamp_at_startup(tmp_path, monkeypatch):
    """Boundary of the digest: it is a snapshot of the source tree at startup,
    not an identity of every module subsequently executed. A module imported
    after an edit runs the new code while the stamp keeps naming the tree as
    it stood at startup. Dependencies and configuration are outside its scope.
    """
    pkg = tmp_path / "latepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    late = pkg / "late.py"
    late.write_text("VALUE = 1\n")

    startup = tmp_path / "startup_snapshot"
    startup.mkdir()
    (startup / "__init__.py").write_text("")
    (startup / "late.py").write_text("VALUE = 1\n")
    stamp = _source_digest(startup)  # what the process records at import

    late.write_text("VALUE = 2\n")  # the edit lands after startup
    monkeypatch.syspath_prepend(str(tmp_path))
    module = import_module("latepkg.late")

    assert module.VALUE == 2  # the process runs the edited module...
    assert _source_digest(pkg) != stamp  # ...which a fresh hash would notice...
    assert stamp == _source_digest(startup)  # ...while the startup stamp holds
