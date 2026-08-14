"""Static security guards for the packaged Docker runtime image."""

import shlex
from pathlib import Path

import pytest


DOCKERFILE = (
    Path(__file__).resolve().parent.parent
    / "containers" / "docker" / "Dockerfile"
)
pytestmark = pytest.mark.regression


def _logical_instructions(dockerfile: str) -> list[str]:
    """Return Dockerfile instructions with line continuations normalized."""
    return [
        " ".join(instruction.split())
        for instruction in dockerfile.replace("\\\n", " ").splitlines()
        if instruction.strip() and not instruction.lstrip().startswith("#")
    ]


def test_shadow_layout_supports_apparmor_confined_unix_chkpwd():
    """The image must not need host DAC capabilities for PAM account checks.

    Ubuntu 24.04's AppArmor profile permits ``/etc/shadow r`` but strips
    ``dac_override`` and ``dac_read_search`` from ``unix_chkpwd``. A root
    owner read bit lets the setuid-root helper pass ordinary DAC without
    granting the SSH login user direct DAC access or using a readable group.
    The development image separately grants that user passwordless sudo.
    """
    instructions = _logical_instructions(DOCKERFILE.read_text())
    useradd_index, useradd = next(
        (index, instruction)
        for index, instruction in enumerate(instructions)
        if "useradd" in instruction and "qemu_user" in instruction)
    shadow_index, shadow = next(
        (index, instruction)
        for index, instruction in enumerate(instructions)
        if "/etc/shadow" in instruction)

    assert useradd_index < shadow_index
    assert shlex.split(useradd.removeprefix("RUN ").split(" && ")[0]) == [
        "useradd", "-m", "-s", "/bin/bash", "qemu_user"]
    assert [
        shlex.split(command)
        for command in shadow.removeprefix("RUN ").split(" && ")
    ] == [
        ["chown", "root:root", "/etc/shadow"],
        ["chmod", "0400", "/etc/shadow"],
        ["test", "$(stat -c '%U:%G:%a' /etc/shadow)", "=", "root:root:400"],
        ["!", "id", "-Gn", "qemu_user", "|", "grep", "-qw", "shadow"],
    ]
