"""
Packaging metadata guards.

These assert properties of ``pyproject.toml`` that only go wrong in the
*built artifact*, where neither the source tree nor the rest of the suite
would notice. The motivating bug (found in Phase 9, issue #57):

    [tool.poetry.extras]
    docker-compose = ["docker"]

    [tool.poetry.group.runtime-docker.dependencies]
    docker = ">=7.0"

Poetry resolves ``extras`` against ``[tool.poetry.dependencies]`` only, so
declaring ``docker`` in a *group* made the extra a silent no-op: the wheel
published ``Provides-Extra: docker-compose`` with no matching
``Requires-Dist``, and ``pip install 'boxman[docker-compose]'`` — the command
the README gives — installed nothing at all. It failed only at runtime, as a
``ModuleNotFoundError`` inside Ansible's ``community.docker`` plugin.
"""

from __future__ import annotations

import os

import pytest

tomllib = pytest.importorskip(
    "tomllib", reason="stdlib tomllib needs Python 3.11+ (project supports 3.10)")

PYPROJECT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")


@pytest.fixture(scope="module")
def pyproject():
    with open(PYPROJECT, "rb") as fobj:
        return tomllib.load(fobj)


def test_every_extra_dependency_is_declared_and_optional(pyproject):
    """Each name in ``[tool.poetry.extras]`` must be declared in
    ``[tool.poetry.dependencies]`` **and** marked ``optional = true``.

    Anything else builds cleanly and installs nothing.
    """
    poetry = pyproject["tool"]["poetry"]
    extras = poetry.get("extras", {})
    deps = poetry.get("dependencies", {})
    if not extras:
        pytest.skip("no extras declared")

    problems = []
    for extra_name, members in extras.items():
        for dep in members:
            spec = deps.get(dep)
            if spec is None:
                problems.append(
                    f"extra '{extra_name}' lists '{dep}', which is not in "
                    f"[tool.poetry.dependencies] — the extra will install nothing"
                )
            elif not (isinstance(spec, dict) and spec.get("optional") is True):
                problems.append(
                    f"extra '{extra_name}' lists '{dep}', which is declared but "
                    f"not 'optional = true' — it would become a hard dependency"
                )
    assert not problems, "\n".join(problems)


def test_docker_compose_extra_covers_the_docker_sdk(pyproject):
    """The documented ``pip install '.[docker-compose]'`` must deliver the
    Docker SDK, which Ansible's ``community.docker`` connection plugin needs
    to reach containers of a docker-compose cluster."""
    poetry = pyproject["tool"]["poetry"]
    assert "docker" in poetry.get("extras", {}).get("docker-compose", []), (
        "the docker-compose extra no longer provides the docker SDK; "
        "README and doc/docker-compose-provider/user-guide.md both tell users "
        "to install it this way")


def test_every_file_the_dockerfile_copies_ships_in_the_package(pyproject):
    """``include`` is an explicit per-file list, and the runtime builds the
    image from the copy of ``containers/docker`` that shipped. A file the
    Dockerfile COPYs but the list leaves out exists in a checkout and not in
    a wheel install, where the image build then fails (#205 added
    ``seed-libvirt-state.sh``)."""
    docker_dir = os.path.join(os.path.dirname(PYPROJECT), "containers", "docker")
    with open(os.path.join(docker_dir, "Dockerfile")) as fobj:
        copied = [line.split()[1] for line in fobj
                  if line.startswith("COPY ") and "--from" not in line]
    assert copied, "found no COPY lines in the Dockerfile"

    shipped = {entry["path"] if isinstance(entry, dict) else entry
               for entry in pyproject["tool"]["poetry"].get("include", [])}
    missing = [src for src in copied
               if f"containers/docker/{src}" not in shipped]
    assert not missing, (
        f"the Dockerfile copies {missing}, which pyproject.toml does not "
        f"include in the package")
