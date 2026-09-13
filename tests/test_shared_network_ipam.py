"""One alias must not mean two L2s, and declared IPAM must hold together.

#164 NET-C3 and FBN-16.

``_service_networks`` resolves cluster-internal names before shared ones, so
a name declared in both silently put the box on an isolated bridge instead of
the shared L2 it asked for. And ``_require_macvlan_ipam`` checked only that
``bridge:`` and ``subnet:`` were *present* — a gateway outside the subnet, or
an ``ip_range`` that is not inside it, sailed through to a late and obscure
docker error.
"""

from unittest import mock

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.docker_compose.compose_generator import ComposeGenerator

pytestmark = pytest.mark.unit

SHARED = {"lab": {"bridge": "br-lab", "subnet": "10.10.0.0/24"}}


def _generate(cluster, shared=None):
    return ComposeGenerator().generate(
        "proj", cluster, conf_dir="/proj", shared_networks=shared or SHARED)


def _cluster(networks, box_networks, extra=None, cluster_extra=None):
    box = {"image": "x"}
    if box_networks is not None:
        box["networks"] = box_networks
    if extra:
        box["compose_extra"] = extra
    cluster = {"networks": networks, "boxes": {"web": box}}
    if cluster_extra:
        cluster["compose_extra"] = cluster_extra
    return cluster


class TestAnAliasMayNotMeanTwoL2s:
    """A name in both `networks:` and `shared_networks:` is refused."""

    def test_an_ambiguous_attachment_is_refused(self):
        with pytest.raises(ConfigError, match=r"names both"):
            _generate(_cluster({"lab": {}}, ["lab"]))

    def test_the_message_names_both_declarations(self):
        with pytest.raises(ConfigError) as exc:
            _generate(_cluster({"lab": {}}, ["lab"]))

        text = str(exc.value)
        assert "cluster-internal" in text
        assert "shared_networks" in text
        assert "br-lab" in text

    def test_an_unused_colliding_alias_is_fine(self):
        """The collision only matters if something attaches to it."""
        out = _generate(_cluster({"lab": {}, "other": {}}, ["other"]))

        assert out["services"]["web"]["networks"] == ["other"]

    def test_a_network_mode_override_removes_the_ambiguity(self):
        """`network_mode:` takes the attachments with it, so nothing lands."""
        out = _generate(_cluster({"lab": {}}, ["lab"],
                                 extra={"network_mode": "host"}))

        assert "networks" not in out["services"]["web"]

    def test_an_override_that_drops_the_attachment_is_fine(self):
        out = _generate(_cluster({"lab": {}}, ["lab"],
                                 cluster_extra={
                                     "services": {"web": {"networks": []}}}))

        assert not out["services"]["web"].get("networks")

    def test_an_unrelated_include_does_not_excuse_it(self):
        """Compose is no oracle here — it knows nothing of shared_networks.

        NET-C1 defers an unresolved reference when the file carries an
        `include:`, because Compose refuses an undeclared network itself. It
        cannot refuse this: the ambiguous name resolves perfectly well, to
        the wrong L2 (#164 NET-C3).
        """
        with pytest.raises(ConfigError, match=r"names both"):
            _generate(_cluster({"lab": {}}, ["lab"],
                               cluster_extra={"include": ["extra.yml"]}))

    def test_a_shared_only_name_still_attaches(self):
        out = _generate(_cluster({}, ["lab"]))

        assert out["services"]["web"]["networks"] == ["lab"]
        assert out["networks"]["lab"]["driver"] == "macvlan"


class TestDeclaredIpamMustHoldTogether:
    """Presence is not membership (#164 FBN-16)."""

    def _shared(self, **over):
        entry = {"bridge": "br-lab", "subnet": "10.10.0.0/24"}
        entry.update(over)
        return {"lab": entry}

    def test_a_gateway_outside_the_subnet_is_refused(self):
        with pytest.raises(ConfigError, match=r"gateway.*outside"):
            _generate(_cluster({}, ["lab"]),
                      shared=self._shared(gateway="10.99.0.1"))

    def test_an_ip_range_outside_the_subnet_is_refused(self):
        with pytest.raises(ConfigError, match=r"ip_range.*not inside"):
            _generate(_cluster({}, ["lab"]),
                      shared=self._shared(ip_range="10.99.0.0/28"))

    def test_a_malformed_subnet_is_refused(self):
        with pytest.raises(ConfigError, match=r"not a valid network"):
            _generate(_cluster({}, ["lab"]),
                      shared=self._shared(subnet="10.10.0.0/33"))

    def test_a_gateway_and_range_inside_the_subnet_pass(self):
        out = _generate(_cluster({}, ["lab"]),
                        shared=self._shared(gateway="10.10.0.1",
                                            ip_range="10.10.0.128/25"))

        cfg = out["networks"]["lab"]["ipam"]["config"][0]
        assert cfg["gateway"] == "10.10.0.1"
        assert cfg["ip_range"] == "10.10.0.128/25"

    def test_an_interpolated_subnet_defers_its_comparisons(self):
        """Skipping the subnet must not turn its dependents into errors."""
        out = _generate(_cluster({}, ["lab"]),
                        shared=self._shared(subnet="${SUBNET}",
                                            gateway="10.10.0.1",
                                            ip_range="10.10.0.128/25"))

        assert out["networks"]["lab"]["ipam"]["config"][0]["subnet"] == \
            "${SUBNET}"

    def test_an_interpolated_gateway_defers(self):
        out = _generate(_cluster({}, ["lab"]),
                        shared=self._shared(gateway="${GW}"))

        assert out["networks"]["lab"]["ipam"]["config"][0]["gateway"] == "${GW}"

    def test_an_override_may_replace_a_bad_raw_gateway(self):
        """Checked after the merge, so an override can correct it."""
        out = _generate(
            _cluster({}, ["lab"],
                     cluster_extra={"networks": {"lab": {"ipam": {"config": [
                         {"subnet": "10.10.0.0/24",
                          "gateway": "10.10.0.1"}]}}}}),
            shared=self._shared(gateway="10.99.0.1"))

        cfg = out["networks"]["lab"]["ipam"]["config"][0]
        assert cfg["gateway"] == "10.10.0.1"


class TestAMissingIpRangeIsWarnedAbout:
    """Docker then owns the whole subnet and starts at the first free address.

    Its IPAM sees only its own pools — never the wire — so on a shared L2 it
    will hand a container an address a VM or a DHCP server already holds
    (#164 NET-C2).
    """

    def test_it_warns(self):
        gen = ComposeGenerator()
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", _cluster({}, ["lab"]), conf_dir="/proj",
                         shared_networks=SHARED)

        said = " ".join(str(c) for c in warn.call_args_list)
        assert "ip_range" in said

    def test_declaring_a_range_silences_it(self):
        gen = ComposeGenerator()
        shared = {"lab": {"bridge": "br-lab", "subnet": "10.10.0.0/24",
                          "ip_range": "10.10.0.128/25"}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", _cluster({}, ["lab"]), conf_dir="/proj",
                         shared_networks=shared)

        said = " ".join(str(c) for c in warn.call_args_list)
        assert "ip_range" not in said

    def test_an_unreferenced_shared_network_is_not_warned_about(self):
        """Only networks this cluster actually uses."""
        gen = ComposeGenerator()
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", _cluster({"a": {}}, ["a"]), conf_dir="/proj",
                         shared_networks=SHARED)

        assert not any("ip_range" in str(c) for c in warn.call_args_list)


class TestValidationFollowsEffectiveUse:
    """`referenced_shared` is what was asked for *before* overrides merged.

    Validating against it rejects configurations that deploy: an override
    may remove the attachment entirely, and Compose then drops the unused
    network from its resolved model (#164 FBN-16).
    """

    def _bad_gateway(self):
        return {"lab": {"bridge": "br-lab", "subnet": "10.10.0.0/24",
                        "gateway": "10.99.0.1"}}

    def test_network_mode_host_removes_the_network_from_use(self):
        out = _generate(_cluster({}, ["lab"],
                                 extra={"network_mode": "host"}),
                        shared=self._bad_gateway())

        assert "networks" not in out["services"]["web"]

    def test_an_override_clearing_the_attachment_removes_it(self):
        out = _generate(_cluster({}, ["lab"],
                                 cluster_extra={"services": {
                                     "web": {"networks": []}}}),
                        shared=self._bad_gateway())

        assert not out["services"]["web"].get("networks")

    def test_a_profiled_service_does_not_trigger_validation(self):
        """Whether its profile is active is not decidable here."""
        out = _generate(_cluster({}, ["lab"],
                                 extra={"profiles": ["debug"]}),
                        shared=self._bad_gateway())

        assert out["services"]["web"]["networks"] == ["lab"]

    def test_a_network_still_in_use_is_still_validated(self):
        with pytest.raises(ConfigError, match=r"gateway.*outside"):
            _generate(_cluster({}, ["lab"]), shared=self._bad_gateway())


class TestAutomaticIpamIsPreserved:
    """`ipam: {config: [{}]}` asks docker to select a predefined pool."""

    def test_an_empty_pool_request_is_not_rejected(self):
        out = _generate(
            _cluster({}, ["lab"],
                     cluster_extra={"networks": {"lab": {
                         "ipam": {"config": [{}]}}}}))

        assert out["networks"]["lab"]["ipam"]["config"] == [{}]

    def test_it_is_not_warned_about_either(self):
        gen = ComposeGenerator()
        cluster = _cluster({}, ["lab"],
                           cluster_extra={"networks": {"lab": {
                               "ipam": {"config": [{}]}}}})
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", cluster, conf_dir="/proj",
                         shared_networks=SHARED)

        assert not any("ip_range" in str(c) for c in warn.call_args_list)


class TestTheMissingRangeWarningDoesNotDependOnParsing:
    """It is the whole NET-C2 safeguard, so a deferred membership
    comparison must not take it down with it (#164 NET-C2)."""

    def test_an_interpolated_subnet_still_warns(self):
        gen = ComposeGenerator()
        shared = {"lab": {"bridge": "br-lab", "subnet": "${SUBNET}"}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("proj", _cluster({}, ["lab"]), conf_dir="/proj",
                         shared_networks=shared)

        assert any("ip_range" in str(c) for c in warn.call_args_list)


class TestAddressFamilyMismatchIsADiagnostic:
    """It used to raise an uncaught TypeError from the containment test."""

    def test_an_ipv6_range_against_an_ipv4_subnet(self):
        shared = {"lab": {"bridge": "br-lab", "subnet": "10.10.0.0/24",
                          "ip_range": "fd00::/64"}}
        with pytest.raises(ConfigError, match=r"IPv6.*IPv4"):
            _generate(_cluster({}, ["lab"]), shared=shared)

    def test_an_ipv6_gateway_against_an_ipv4_subnet(self):
        shared = {"lab": {"bridge": "br-lab", "subnet": "10.10.0.0/24",
                          "gateway": "fd00::1"}}
        with pytest.raises(ConfigError, match=r"IPv6.*IPv4"):
            _generate(_cluster({}, ["lab"]), shared=shared)


class TestProvenanceIsEmittedForInterpolatedReferences:
    """`${LAN}` hides which network it means until compose interpolates it.

    boxman must not interpolate anything itself, so the references go out
    verbatim in an extension compose resolves and hands back (#164 NET-C3).
    """

    KEY = "x-boxman-net-provenance"

    def test_nothing_is_emitted_without_an_ambiguous_alias(self):
        out = _generate(_cluster({}, ["lab"]))

        assert self.KEY not in out

    def test_the_ambiguous_set_comes_from_declarations(self):
        """Not from references that literally matched both.

        `${LAN}` matches nothing literally — keying emission off matched
        references would emit nothing and leave the bypass exactly as it was.
        """
        out = _generate(_cluster({"lab": {}}, ["${LAN}"]))

        assert out[self.KEY]["ambiguous"] == ["lab"]
        assert out[self.KEY]["native"] == {"web": ["${LAN}"]}

    def test_a_reference_removed_by_network_mode_is_not_carried(self):
        """A stale `${VAR:?err}` makes compose fail interpolating the
        extension itself."""
        out = _generate(_cluster({"lab": {}}, ["${REQUIRED:?missing}"],
                                 extra={"network_mode": "host"}))

        assert self.KEY not in out

    def test_a_reference_removed_by_an_override_is_not_carried(self):
        out = _generate(_cluster({"lab": {}}, ["${LAN}"],
                                 cluster_extra={"services": {
                                     "web": {"networks": []}}}))

        assert self.KEY not in out

    def test_a_service_with_no_surviving_reference_is_omitted(self):
        """Compose turns an empty list into `null` on the way back."""
        cluster = _cluster({"lab": {}}, ["${LAN}"])
        cluster["boxes"]["idle"] = {"image": "x"}

        out = _generate(cluster)

        assert set(out[self.KEY]["native"]) == {"web"}

    def test_only_the_references_that_survive_are_carried(self):
        """An override may keep some attachments and drop others.

        Carrying a dropped reference would falsely accuse the service, and a
        dropped `${VAR:?err}` would make compose fail interpolating the
        extension itself (#164 NET-C3).
        """
        out = _generate(_cluster({"lab": {}, "keep": {}},
                                 ["${LAN}", "keep"],
                                 cluster_extra={"services": {
                                     "web": {"networks": ["keep"]}}}))

        assert out[self.KEY]["native"] == {"web": ["keep"]}
