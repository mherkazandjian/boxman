"""
An explicit network reference must be honoured or refused (#164 NET-C1).

An unresolvable name used to warn and be dropped. The service was then
left with no explicit attachment, and Compose places such a service on the
project's default network — so a one-character typo silently changed which
L2 a container joined:

    db:  {networks: [app_bridge]}    # isolated, as asked
    web: {networks: [app_brige]}     # typo -> project default network

``web`` shares that default network with other services that also have no
explicit attachment. It does *not* join ``db``'s network — the failure is
that ``web`` is not where it was declared to be, not that the two become
adjacent.

The literal ``default`` is a real Compose network that needs no top-level
declaration, so it resolves rather than raising; names boxman does not
manage reach Compose through ``compose_extra:``, which bypasses this
resolver entirely.
"""

from unittest import mock

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.docker_compose.compose_generator import ComposeGenerator

pytestmark = pytest.mark.unit


def _cluster(networks, extra=None):
    box = {"image": "x"}
    if networks is not None:
        box["networks"] = networks
    if extra:
        box["compose_extra"] = extra
    return {
        "networks": {"app_bridge": {"subnet": "10.9.0.0/24"}},
        "boxes": {"web": box},
    }


def _generate(cluster, shared=None):
    return ComposeGenerator().generate(
        "proj", cluster, conf_dir="/proj", shared_networks=shared or {})


class TestAnUnresolvableReferenceIsRefused:

    @pytest.mark.parametrize("declared, form", [
        (["ghost"], "list"),
        ("ghost", "bare string"),
        ({"ghost": {"ipv4_address": "10.9.0.5"}}, "mapping"),
    ])
    def test_every_declaration_form_raises(self, declared, form):
        with pytest.raises(ConfigError, match="ghost"):
            _generate(_cluster(declared))

    def test_a_mixed_list_raises_rather_than_partly_attaching(self):
        """Attaching the good ones would silently reduce connectivity."""
        with pytest.raises(ConfigError, match="ghost"):
            _generate(_cluster(["app_bridge", "ghost"]))

    def test_the_message_names_the_box_and_what_is_available(self):
        with pytest.raises(ConfigError) as excinfo:
            _generate(_cluster(["ghost"]))

        message = str(excinfo.value)
        assert "proj.web" in message
        assert "ghost" in message
        assert "app_bridge" in message      # what it could have meant
        assert "compose_extra" in message   # the escape hatch

    def test_a_shared_network_name_resolves(self):
        shared = {"labnet": {"bridge": "br-lab", "subnet": "10.8.0.0/24",
                             "gateway": "10.8.0.1"}}
        out = _generate(_cluster(["labnet"]), shared=shared)

        assert out["services"]["web"]["networks"] == ["labnet"]


class TestTheImplicitDefaultNetworkResolves:
    """Compose accepts `networks: [default]` with no top-level declaration."""

    def test_default_alone_is_accepted(self):
        """Accepted, and emitted the same way an omitted ``networks:`` is.

        ``networks: [default]`` and no ``networks:`` key put the service on
        exactly the same network, so boxman emits the shorter one. The
        redundant form is also the only thing that turns a ``network_mode:``
        override into Compose's "mutually exclusive" error -- including a
        mode boxman cannot see, inherited via ``extends:`` or written as
        ``${VAR:-none}`` (#164 NET-C1).
        """
        out = _generate(_cluster(["default"]))

        assert "networks" not in out["services"]["web"]
        assert out["services"]["web"] == _generate(
            _cluster(None))["services"]["web"]

    def test_default_alongside_a_declared_network(self):
        out = _generate(_cluster(["app_bridge", "default"]))

        assert out["services"]["web"]["networks"] == ["app_bridge", "default"]

    def test_default_is_not_emitted_as_a_top_level_network(self):
        """Compose provides it; declaring it would be boxman's invention."""
        out = _generate(_cluster(["default"]))

        assert "default" not in (out.get("networks") or {})


class TestOmittedAndEmptyMeanTheDefaultNetwork:
    """Not "no networking" — Compose attaches the default network.

    To have none, use ``compose_extra: {network_mode: none}``.
    """

    @pytest.mark.parametrize("declared", [None, []])
    def test_no_networks_key_is_emitted(self, declared):
        out = _generate(_cluster(declared))

        assert "networks" not in out["services"]["web"]

    def test_neither_warns(self):
        gen = ComposeGenerator()
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", _cluster([]), conf_dir="/proj")

        assert not warn.called


class TestOverridesAreAccountedForBeforeRefusing:
    """The check runs on the assembled file, after every override.

    An earlier version rejected an unresolved name the moment it was read,
    which fired before `compose_extra:` could supply or remove it — and
    refused two configurations codex reproduced as working on the base
    revision inside the VM (#164 NET-C1, implementation review).
    """

    def test_a_native_ref_satisfied_by_a_cluster_override(self):
        """codex regression 1: attach natively, declare via the hatch."""
        cluster = _cluster(["corp"], extra={"networks": ["corp"]})
        cluster["compose_extra"] = {"networks": {"corp": {"driver": "bridge"}}}

        out = _generate(cluster)

        assert out["services"]["web"]["networks"] == ["corp"]
        assert out["networks"]["corp"] == {"driver": "bridge"}

    def test_a_native_ref_satisfied_by_a_cluster_level_service_override(self):
        """The same regression through cluster.compose_extra.services."""
        cluster = _cluster(["corp"])
        cluster["compose_extra"] = {
            "services": {"web": {"networks": ["corp"]}},
            "networks": {"corp": {"driver": "bridge"}},
        }

        out = _generate(cluster)

        assert out["services"]["web"]["networks"] == ["corp"]

    def test_network_mode_takes_the_attachments_with_it(self):
        """codex regression 2: `network_mode` and `networks` are exclusive.

        Emitting `networks: [default]` alongside a `network_mode: none`
        override made Compose exit 1 with "mutually exclusive
        network_mode and networks" — on a configuration that worked.
        """
        out = _generate(_cluster(["default"],
                                 extra={"network_mode": "none"}))

        svc = out["services"]["web"]
        assert svc["network_mode"] == "none"
        assert "networks" not in svc

    def test_network_mode_also_wins_over_a_declared_network(self):
        out = _generate(_cluster(["app_bridge"],
                                 extra={"network_mode": "host"}))

        assert "networks" not in out["services"]["web"]

    def test_an_unsatisfied_ref_is_still_refused(self):
        """The hatch has to actually supply it — presence is not enough."""
        with pytest.raises(ConfigError, match="someone-elses"):
            _generate(_cluster(["someone-elses"],
                               extra={"networks": ["someone-elses"]}))


class TestValidClustersAreUnaffected:

    def test_output_is_unchanged_for_a_good_config(self):
        out = _generate(_cluster(["app_bridge"]))

        assert out["services"]["web"]["networks"] == ["app_bridge"]
        assert "app_bridge" in out["networks"]


class TestAFailedGenerationChangesNothing:
    """The refusal must not cost the cluster its working compose file.

    ``_compose_context`` generates *before* writing, so a ConfigError means
    ``write()`` never runs and no runner is built — the existing file
    stands and bring-up is never invoked. That ordering is easy to break
    later, so it is pinned here (#164 NET-C1).
    """

    def _session(self, tmp_path):
        from boxman.providers.docker_compose.session import DockerComposeSession

        session = DockerComposeSession({})
        session.logger = mock.MagicMock()
        session.use_sudo = False
        session._generator = ComposeGenerator()
        session._workdir = lambda cfg, name: str(tmp_path)
        session._conf_dir = lambda: str(tmp_path)
        session._compose_project = lambda name: "proj"
        return session

    def test_an_existing_compose_file_survives(self, tmp_path):
        existing = tmp_path / "docker-compose.yml"
        existing.write_text("# the previous, working file\n")
        session = self._session(tmp_path)

        with pytest.raises(ConfigError, match="ghost"):
            session._compose_context("proj", _cluster(["ghost"]))

        assert existing.read_text() == "# the previous, working file\n"

    def test_the_generator_never_writes(self, tmp_path):
        session = self._session(tmp_path)
        with mock.patch.object(session._generator, "write") as write:
            with pytest.raises(ConfigError):
                session._compose_context("proj", _cluster(["ghost"]))

        write.assert_not_called()

    def test_a_valid_cluster_still_writes(self, tmp_path):
        """Reachability: the path does work when the config is good."""
        session = self._session(tmp_path)

        runner, workdir, compose_file = session._compose_context(
            "proj", _cluster(["app_bridge"]))

        assert (tmp_path / "docker-compose.yml").exists()
        assert compose_file.endswith("docker-compose.yml")
        assert runner is not None


def _cluster_extra(cluster, extra):
    """Attach a cluster-level ``compose_extra:`` to a cluster."""
    cluster = dict(cluster)
    cluster["compose_extra"] = extra
    return cluster


class TestOnlyAModeInEffectTakesTheAttachments:
    """`network_mode:` present is not the same as a mode in effect.

    Treating any non-None value as a mode erased declared attachments —
    ``network_mode: ""`` both hid an unresolvable reference from the check
    and silently detached a service from a real network (#164 NET-C1).
    """

    def test_an_empty_mode_does_not_hide_an_unresolvable_reference(self):
        with pytest.raises(ConfigError, match=r"network 'ghost'"):
            _generate(_cluster(["ghost"], extra={"network_mode": ""}))

    def test_an_empty_mode_does_not_detach_a_declared_network(self):
        out = _generate(_cluster(["app_bridge"], extra={"network_mode": ""}))

        assert out["services"]["web"]["networks"] == ["app_bridge"]

    def test_a_literal_mode_takes_the_attachments(self):
        out = _generate(_cluster(["app_bridge"], extra={"network_mode": "none"}))

        assert "networks" not in out["services"]["web"]

    def test_a_cluster_level_mode_is_seen(self):
        """The cleanup used to run inside `_service()`, before this merged."""
        out = _generate(_cluster_extra(
            _cluster(["app_bridge"]),
            {"services": {"web": {"network_mode": "none"}}}))

        assert "networks" not in out["services"]["web"]

    def test_a_mode_cancelled_at_cluster_level_keeps_the_attachment(self):
        out = _generate(_cluster_extra(
            _cluster(["app_bridge"], extra={"network_mode": "none"}),
            {"services": {"web": {"network_mode": ""}}}))

        assert out["services"]["web"]["networks"] == ["app_bridge"]

    def test_an_interpolated_mode_is_left_to_compose(self):
        """Only Compose can resolve it; it may interpolate to empty."""
        out = _generate(_cluster(
            ["app_bridge"], extra={"network_mode": "${MODE:-none}"}))

        assert out["services"]["web"]["networks"] == ["app_bridge"]
        assert out["services"]["web"]["network_mode"] == "${MODE:-none}"


class TestTheRedundantDefaultAttachmentIsNotEmitted:
    """`networks: [default]` is emitted as no `networks:` key at all.

    Both put the service on the project default network, but only the
    explicit form collides with a `network_mode:` boxman cannot see — one
    inherited through `extends:` or written as `${VAR:-none}`. Not emitting
    it removes that whole class of invalid output (#164 NET-C1).
    """

    def test_a_mode_inherited_through_extends_cannot_collide(self):
        out = _generate(_cluster(
            ["default"],
            extra={"extends": {"file": "parent.yml", "service": "base"}}))

        assert "networks" not in out["services"]["web"]

    def test_an_interpolated_mode_cannot_collide(self):
        out = _generate(_cluster(
            ["default"], extra={"network_mode": "${MODE:-none}"}))

        assert "networks" not in out["services"]["web"]

    def test_default_alongside_a_real_network_is_still_emitted(self):
        out = _generate(_cluster(["app_bridge", "default"]))

        assert out["services"]["web"]["networks"] == ["app_bridge", "default"]


class TestReferencesComposeResolvesAreDeferredNotRefused:
    """Compose performs `include:`, `extends:` and `${VAR}` before it checks.

    It then refuses any service reference to an undeclared network itself, so
    a reference boxman cannot resolve is emitted and left to Compose — which
    is verified on a candidate file before it replaces the working one. What
    boxman must never do is *drop* the reference, which is what silently put
    the service on the default network (#164 NET-C1).
    """

    def test_an_interpolated_reference_is_not_refused(self):
        out = _generate(_cluster(None, extra={"networks": ["${NET:-corp}"]}))

        assert out["services"]["web"]["networks"] == ["${NET:-corp}"]

    def test_a_native_interpolated_reference_is_not_refused(self):
        out = _generate(_cluster(["${NET:-app_bridge}"]))

        assert out["services"]["web"]["networks"] == ["${NET:-app_bridge}"]

    def test_an_include_may_define_the_network(self):
        cluster = _cluster_extra(_cluster(["corp"]), {"include": ["extra.yml"]})

        out = _generate(cluster)

        assert out["services"]["web"]["networks"] == ["corp"]

    def test_extends_makes_the_service_not_fully_visible(self):
        out = _generate(_cluster(
            ["corp"],
            extra={"extends": {"file": "parent.yml", "service": "base"}}))

        assert out["services"]["web"]["networks"] == ["corp"]

    def test_a_reference_added_by_compose_extra_is_not_judged(self):
        """`compose_extra:` is the user deliberately reaching past boxman."""
        out = _generate(_cluster(None, extra={"networks": ["corp"]}))

        assert out["services"]["web"]["networks"] == ["corp"]

    def test_an_unresolvable_native_reference_is_still_refused(self):
        """The deferrals above must not blunt the ordinary typo."""
        with pytest.raises(ConfigError, match=r"network 'app_brige'"):
            _generate(_cluster(["app_brige"]))


class TestADeclaredDefaultIsARealNetwork:
    """Only the *implicit* default is redundant.

    A `default` the cluster declares is a network like any other, and under
    `extends:` an omitted `networks:` means "inherit the parent's
    attachments" — so dropping the explicit entry silently changes where the
    service lands (#164 NET-C1).
    """

    @staticmethod
    def _declared_default(box_extra=None):
        box = {"image": "x", "networks": ["default"]}
        if box_extra:
            box["compose_extra"] = box_extra
        return {"networks": {"default": {}, "backend": {}},
                "boxes": {"web": box}}

    def test_a_declared_default_is_emitted(self):
        out = _generate(self._declared_default())

        assert out["services"]["web"]["networks"] == ["default"]

    def test_a_declared_default_survives_extends(self):
        """Omitting it here would inherit the parent's networks instead."""
        out = _generate(self._declared_default(
            {"extends": {"service": "base"}}))

        assert out["services"]["web"]["networks"] == ["default"]

    def test_a_declared_default_survives_an_empty_mode(self):
        out = _generate(self._declared_default({"network_mode": ""}))

        assert out["services"]["web"]["networks"] == ["default"]

    def test_a_declared_default_is_emitted_as_a_top_level_network(self):
        out = _generate(self._declared_default())

        assert "default" in out["networks"]


class TestAStaticAddressOnTheImplicitDefaultIsMeaningless:
    """boxman declares no IPAM pool for Compose's implicit default.

    Keeping the option kept the mapping form alive, which put `networks:`
    back alongside a `network_mode:` boxman cannot see — the collision the
    elision exists to prevent (#164 NET-C1).
    """

    def _cluster_with_pinned_default(self):
        return {
            "networks": {"app_bridge": {"subnet": "10.9.0.0/24"}},
            "boxes": {"web": {
                "image": "x",
                "networks": {"default": {"ipv4_address": "172.30.0.5"}},
                "compose_extra": {"network_mode": "${MODE:-none}"},
            }},
        }

    def test_the_address_is_dropped_and_warned_about(self):
        gen = ComposeGenerator()
        with mock.patch.object(gen.logger, "warning") as warn:
            out = gen.generate("proj", self._cluster_with_pinned_default(),
                               conf_dir="/proj")

        assert "networks" not in out["services"]["web"]
        assert any("ipv4_address" in str(c) for c in warn.call_args_list)

    def test_an_invisible_mode_cannot_collide(self):
        out = _generate(self._cluster_with_pinned_default())

        assert "networks" not in out["services"]["web"]
        assert out["services"]["web"]["network_mode"] == "${MODE:-none}"
