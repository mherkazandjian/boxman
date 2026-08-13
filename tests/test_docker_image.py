"""Static security guards for the packaged Docker runtime image."""

from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parent.parent / "containers" / "docker" / "Dockerfile"


def test_shadow_layout_supports_apparmor_confined_unix_chkpwd():
    """The image must not need host DAC capabilities for PAM account checks.

    Ubuntu 24.04's AppArmor profile permits ``/etc/shadow r`` but strips
    ``dac_override`` and ``dac_read_search`` from ``unix_chkpwd``. A root
    owner read bit lets the setuid-root helper pass ordinary DAC without
    granting the SSH login user direct DAC access or using a readable group.
    The development image separately grants that user passwordless sudo.
    """
    dockerfile = DOCKERFILE.read_text()
    expected = (
        "chown root:root /etc/shadow",
        "chmod 0400 /etc/shadow",
        "test \"$(stat -c '%U:%G:%a' /etc/shadow)\" = \"root:root:400\"",
        "! id -Gn qemu_user | grep -qw shadow",
    )

    for command in expected:
        assert command in dockerfile

    assert dockerfile.index("useradd -m -s /bin/bash qemu_user") < dockerfile.index(
        "chown root:root /etc/shadow"
    )
