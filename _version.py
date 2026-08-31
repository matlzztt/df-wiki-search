"""Single source of truth for the release version.

Everything that reports a version reads it from here: `pyproject.toml` via
setuptools' dynamic metadata, `ingest.py` when it stamps `build_meta`, and
`server.py` when `wiki_index_info()` describes itself.

The version an index carries is the version of the release that built it, so a
server can tell the caller when it is serving an index older than itself.
"""

__version__ = "2.0.0"
