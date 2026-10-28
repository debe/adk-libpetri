# spec/

Cross-language specification of adk-libpetri: the contract every language
port (Java, Python, and later ports) implements, stated once, language-neutral.

- [`00-index.md`](00-index.md): the chapters and the requirement-ID scheme.
- `fixtures/nets/*.json`: the canonical structure of every stock subnet.
  Java's `SpecFixturesTest` writes them (`-Dspec.fixtures.write=true`) and
  golden-checks them; Python's `tests/conformance` golden-checks the same
  files, so a topology drift in either port fails that port's build.
- [`coverage-matrix.md`](coverage-matrix.md): requirement -> Java test -> Python test.
- `api/python.txt`: the Python port's public API (every name in a public
  package's `__all__`, with signatures and `@experimental` flags), written
  and golden-checked by `python/tests/test_public_api.py`
  (`PUBLICAPI_WRITE=1` regenerates it), so an API change shows up as a diff.

A behaviour that only one port has (Python's `from_workflow`, say) is still
specified here, marked with the ports that implement it.
