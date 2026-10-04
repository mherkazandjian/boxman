# Tests

Tiers (markers in `pyproject.toml`): `unit`, `smoke`, `regression`, and
`integration` (needs Docker + /dev/kvm — run via the disposable test-runner
VM, `make help`). A default `pytest` run excludes `slow` and `integration`
via `addopts`. Shared fixtures live in `conftest.py`.

## docker, virsh and sudo are for the integration tier only

Around every test not marked `integration`, `conftest.py` puts fakes of
`docker`, `virsh` and `sudo` first on `PATH`. A fake runs nothing: it records
the call and exits 1, and the test then fails at teardown with the calls it
made. The real commands act on the machine running the tests — its
containers, its libvirt domains and networks, or as root — so a default-tier
test that reached them passed or failed depending on the host, and some
could stop a container or write to a domain (#214). Mock the call (the
command-dispatch stand-ins in `test_runtime.py` and
`test_libvirt_state_migration.py` are examples), or mark the test
`integration`. A test that runs a fake on purpose takes the calls back with
the `host_commands` fixture.

## Dead base-image URLs

The example boxes pin point-release cloud images and ISOs, and mirrors
delete those once a release is superseded (Rocky does it on every minor
release). `make check-box-images` renders every `boxes/**/conf.yml`, probes
each template `image.uri` and `isos:` `uri` without downloading it, and
exits non-zero if any is dead. It tells `gone` (404/410: update the uri and
its checksum) apart from `unreachable` (DNS, timeout, refused connection).
Narrow it with `make check-box-images boxes="boxes/<box> ..."`. It needs
network but no libvirt, and it downloads nothing, so it is safe to run on the
host as well as in the test-runner VM.

The provisioning tier (`tests/test_provision_boxes.py`) runs the same probe
before `create-templates` and fails a box whose image is gone with a message
naming the template and URL, rather than a download error deep in the log.
It still fails rather than skips, so a dead image cannot leave CI green.
The checker's own unit tests (`tests/test_check_box_images.py`) mock HTTP.
