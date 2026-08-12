# Tests

Tiers (markers in `pyproject.toml`): `unit`, `smoke`, `regression`, and
`integration` (needs Docker + /dev/kvm — run via the disposable test-runner
VM, `make help`). A default `pytest` run excludes `slow` and `integration`
via `addopts`. Shared fixtures live in `conftest.py`.
