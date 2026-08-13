from typing import Any

from lxml import etree

from boxman import log
from boxman.exceptions import ConfigError, ProvisionError

from .commands import VirshCommand


class VirshEdit:
    """
    Class to edit libvirt domain xml using xpath expressions.
    """

    def __init__(self, provider_config: dict[str, Any] | None = None):
        """
        initialize the virshedit class.

        args:
            provider_config: configuration for the libvirt provider
        """
        #: VirshCommand: the command executor for virsh
        self.virsh = VirshCommand(provider_config)

        #: logging.Logger: the logger instance
        self.logger = log

    def get_domain_xml(self, domain_name: str, inactive: bool = False) -> str:
        """
        Get the xml definition of a domain.

        args:
            domain_name: name of the domain
            inactive: if True, return the persistent (inactive) config
                      rather than the live config. Use this when modifying
                      the persistent config of a running VM so that live-only
                      state is not clobbered.

        returns:
            xml string of the domain definition
        """
        try:
            args = ['dumpxml', domain_name]
            if inactive:
                args.append('--inactive')
            result = self.virsh.execute(*args)
            return result.stdout
        except (RuntimeError, OSError) as exc:
            self.logger.error(f"failed to get xml for domain {domain_name}: {exc}")
            raise ProvisionError(
                f"failed to dump xml for domain '{domain_name}': {exc}"
            ) from exc

    def modify_xml_xpath(self,
                        xml_content: str,
                        modifications: list[tuple[str, str, str]]) -> str:
        """
        Modify xml content using xpath expressions.

        args:
            xml_content: the xml content to modify
            modifications: list of tuples (xpath, attribute_or_text, new_value)
                          use 'text' for element text content
                          use attribute name for attribute values

        returns:
            modified xml content as string
        """
        try:
            # parse xml using lxml for better xpath support
            tree = etree.fromstring(xml_content.encode('utf-8'))

            for xpath, attr_or_text, new_value in modifications:
                # use lxml xpath to find elements
                elements = tree.xpath(xpath)

                if not elements:
                    # special handling for cpu topology - create if it doesn't exist
                    if xpath == '//cpu/topology':
                        cpu_elements = tree.xpath('//cpu')
                        if cpu_elements:
                            cpu_element = cpu_elements[0]
                            # create topology element using xpath-style approach
                            topology_element = etree.Element('topology')
                            cpu_element.append(topology_element)
                            elements = [topology_element]
                            self.logger.info("created new topology element under cpu")
                        else:
                            self.logger.warning("no cpu element found to add topology to")
                            continue
                    else:
                        self.logger.warning(f"no elements found for xpath: {xpath}")
                        continue
                for element in elements:
                    if attr_or_text == 'text':
                        element.text = new_value
                    else:
                        element.set(attr_or_text, new_value)

                self.logger.debug(f"modified {len(elements)} element(s) at {xpath}")

            # return the modified xml as string
            return etree.tostring(tree, encoding='unicode', pretty_print=True)

        except etree.XMLSyntaxError as exc:
            self.logger.error(f"failed to parse xml: {exc}")
            raise
        except Exception as exc:
            self.logger.error(f"failed to modify xml: {exc}")
            raise

    def find_xpath_values(self, xml_content: str, xpath: str) -> list[str]:
        """
        Find values using xpath expressions.

        args:
            xml_content: the xml content to search
            xpath: xpath expression to find values

        returns:
            list of matching values
        """
        try:
            tree = etree.fromstring(xml_content.encode('utf-8'))
            matches = tree.xpath(xpath)

            # convert results to strings
            result = []
            for match in matches:
                if isinstance(match, str):
                    result.append(match)
                elif hasattr(match, 'text') and match.text:
                    result.append(match.text)
                else:
                    result.append(str(match))

            return result

        except etree.XMLSyntaxError as exc:
            self.logger.error(f"failed to parse xml: {exc}")
            raise
        except Exception as exc:
            self.logger.error(f"failed to find xpath values: {exc}")
            raise

    @staticmethod
    def cpu_memory_modifications(cpus: dict[str, int] | None,
                                 memory_mb: int | None,
                                 max_vcpus: int | None,
                                 max_memory_mb: int | None,
                                 logger=log) -> tuple[list[tuple[str, str, str]], bool]:
        """
        Compute the ``(xpath, attr, value)`` modification list for CPU and
        memory configuration.

        The max ceilings (``max_vcpus`` / ``max_memory_mb``) are clamped to
        at least the current values, and the CPU topology is scaled so that
        ``sockets * cores * threads == max_vcpus``.

        Returns:
            ``(modifications, remove_topology)`` — *remove_topology* is
            True when the max vCPU count cannot be expressed with the
            given cores*threads product, in which case the caller must
            drop the ``//cpu/topology`` element from the domain XML before
            applying the modifications.
        """
        modifications: list[tuple[str, str, str]] = []
        remove_topology = False

        # configure cpu if specified
        if cpus:
            sockets = cpus.get('sockets', 1)
            cores = cpus.get('cores', 1)
            threads = cpus.get('threads', 1)
            total_vcpus = sockets * cores * threads

            effective_max_vcpus = max_vcpus or total_vcpus
            if max_vcpus is not None and max_vcpus < total_vcpus:
                logger.warning(
                    f"max_vcpus ({max_vcpus}) < current vcpus ({total_vcpus}), "
                    f"clamping max_vcpus to {total_vcpus}")
                effective_max_vcpus = total_vcpus

            modifications.append(('//vcpu', 'text', str(effective_max_vcpus)))

            if effective_max_vcpus > total_vcpus:
                # libvirt requires topology product == max vcpu count.
                # scale sockets so that sockets * cores * threads == max.
                # if it doesn't divide evenly, remove the topology element.
                cores_x_threads = cores * threads
                if effective_max_vcpus % cores_x_threads == 0:
                    max_sockets = effective_max_vcpus // cores_x_threads
                    modifications.extend([
                        ('//cpu/topology', 'sockets', str(max_sockets)),
                        ('//cpu/topology', 'cores', str(cores)),
                        ('//cpu/topology', 'threads', str(threads))
                    ])
                    logger.info(
                        f"topology adjusted to sockets={max_sockets} "
                        f"cores={cores} threads={threads} to match "
                        f"max_vcpus={effective_max_vcpus}")
                else:
                    # can't express this max with the given cores*threads,
                    # remove topology so libvirt doesn't reject it
                    logger.info(
                        f"removing topology element: max_vcpus "
                        f"({effective_max_vcpus}) not divisible by "
                        f"cores*threads ({cores_x_threads})")
                    remove_topology = True

                modifications.append(
                    ('//vcpu', 'current', str(total_vcpus)))
            else:
                # max == current, set topology normally
                modifications.extend([
                    ('//cpu/topology', 'sockets', str(sockets)),
                    ('//cpu/topology', 'cores', str(cores)),
                    ('//cpu/topology', 'threads', str(threads))
                ])

        # configure memory if specified
        if memory_mb is not None:
            effective_max_memory = max_memory_mb or memory_mb
            if max_memory_mb is not None and max_memory_mb < memory_mb:
                logger.warning(
                    f"max_memory ({max_memory_mb}M) < memory ({memory_mb}M), "
                    f"clamping max_memory to {memory_mb}M")
                effective_max_memory = memory_mb
            max_memory_kb = effective_max_memory * 1024
            current_memory_kb = memory_mb * 1024
            modifications.extend([
                ('//memory', 'text', str(max_memory_kb)),
                ('//currentMemory', 'text', str(current_memory_kb))
            ])

        return modifications, remove_topology

    @staticmethod
    def _drop_topology(xml_content: str) -> str:
        """Remove every ``//cpu/topology`` element from *xml_content*."""
        tree = etree.fromstring(xml_content.encode('utf-8'))
        for topo in tree.xpath('//cpu/topology'):
            topo.getparent().remove(topo)
        return etree.tostring(tree, encoding='unicode', pretty_print=True)

    @staticmethod
    def validate_memballoon_config(config: dict[str, Any]) -> None:
        """
        Validate a memballoon config dict, raising ConfigError on bad values.

        ``free_page_reporting`` must be a real bool (a YAML string like
        ``"false"`` would silently enable it otherwise) and ``stats_period``
        must be a positive integer (bools excluded) or None to remove the
        stats element.
        """
        if not isinstance(config, dict):
            raise ConfigError(
                f"memballoon config must be a mapping, got {type(config).__name__}")
        if 'free_page_reporting' in config and not isinstance(
                config['free_page_reporting'], bool):
            raise ConfigError(
                f"memballoon.free_page_reporting must be a boolean, got "
                f"{config['free_page_reporting']!r}")
        stats_period = config.get('stats_period')
        if stats_period is not None and (
                isinstance(stats_period, bool)
                or not isinstance(stats_period, int)
                or stats_period < 1):
            raise ConfigError(
                f"memballoon.stats_period must be a positive integer (seconds), "
                f"got {stats_period!r}")

    @staticmethod
    def apply_memballoon_to_xml(xml_content: str, config: dict[str, Any]) -> str:
        """
        Apply the memballoon (virtio-balloon) configuration to domain xml.

        Ensures a ``//devices/memballoon`` element exists with
        ``model='virtio'`` (creating it when absent, upgrading ``none``),
        then applies the supported config keys:

        - ``free_page_reporting`` (bool): sets the ``freePageReporting='on|off'``
          attribute
        - ``stats_period`` (int): sets ``<stats period='N'/>``; an explicit
          None removes the ``<stats>`` element

        Keys absent from *config* are left untouched.

        Args:
            xml_content: the domain xml to modify
            config: memballoon configuration dict

        Returns:
            modified xml content as string

        Raises:
            ConfigError: on invalid config values
        """
        VirshEdit.validate_memballoon_config(config)
        tree = etree.fromstring(xml_content.encode('utf-8'))

        memballoons = tree.xpath('//devices/memballoon')
        if memballoons:
            memballoon = memballoons[0]
            if memballoon.get('model') in (None, 'none'):
                memballoon.set('model', 'virtio')
        else:
            devices = tree.xpath('//devices')
            if not devices:
                raise ProvisionError("domain xml has no <devices> element")
            memballoon = etree.SubElement(devices[0], 'memballoon')
            memballoon.set('model', 'virtio')

        if 'free_page_reporting' in config:
            memballoon.set('freePageReporting',
                           'on' if config['free_page_reporting'] else 'off')

        if 'stats_period' in config:
            stats = memballoon.find('stats')
            if config['stats_period'] is None:
                if stats is not None:
                    memballoon.remove(stats)
            else:
                if stats is None:
                    stats = etree.SubElement(memballoon, 'stats')
                stats.set('period', str(config['stats_period']))

        return etree.tostring(tree, encoding='unicode', pretty_print=True)

    def configure_memballoon(self,
                             domain_name: str,
                             config: dict[str, Any] | None) -> bool:
        """
        Configure the virtio-balloon device for a domain.

        Edits the persistent (inactive) config; ``free_page_reporting``
        takes effect from the next boot of the VM.

        Args:
            domain_name: name of the domain
            config: memballoon configuration dict, or None to skip

        Returns:
            True if successful (or nothing to do), False otherwise

        Raises:
            ConfigError: on invalid config values (not swallowed, so a
                         malformed conf.yml fails loudly)
        """
        if not config:
            self.logger.info(f"no memballoon configuration for {domain_name}, skipping")
            return True
        try:
            xml_content = self.get_domain_xml(domain_name, inactive=True)
            modified_xml = self.apply_memballoon_to_xml(xml_content, config)
            return self.redefine_domain(domain_name, modified_xml)
        except ConfigError:
            raise
        except Exception as exc:
            self.logger.error(f"failed to configure memballoon for {domain_name}: {exc}")
            return False

    def configure_cpu_memory(self,
                           domain_name: str,
                           cpus: dict[str, int] | None = None,
                           memory_mb: int | None = None,
                           max_vcpus: int | None = None,
                           max_memory_mb: int | None = None) -> bool:
        """
        Configure cpu and memory settings for a domain.

        args:
            domain_name: name of the domain
            cpus: dictionary with 'sockets', 'cores', 'threads' keys
            memory_mb: current memory in mb
            max_vcpus: maximum vCPU ceiling for hot-scaling (defaults to
                       sockets*cores*threads when omitted)
            max_memory_mb: maximum memory ceiling in mb for hot-scaling
                           (defaults to memory_mb when omitted)

        returns:
            true if successful, false otherwise
        """
        try:
            # get current xml
            xml_content = self.get_domain_xml(domain_name)

            # debug: log current cpu configuration
            tree = etree.fromstring(xml_content.encode('utf-8'))
            cpu_elements = tree.xpath('//cpu')
            if cpu_elements:
                self.logger.debug(f"found cpu element: {etree.tostring(cpu_elements[0], encoding='unicode')}")

            topology_elements = tree.xpath('//cpu/topology')
            if topology_elements:
                self.logger.debug(f"found topology element: {etree.tostring(topology_elements[0], encoding='unicode')}")
            else:
                self.logger.debug("no topology element found")

            modifications, remove_topology = self.cpu_memory_modifications(
                cpus, memory_mb, max_vcpus, max_memory_mb,
                logger=self.logger)

            if remove_topology:
                xml_content = self._drop_topology(xml_content)

            if not modifications:
                self.logger.info(f"no cpu/memory modifications needed for {domain_name}")
                return True

            # apply modifications
            modified_xml = self.modify_xml_xpath(xml_content, modifications)

            # redefine the domain
            return self.redefine_domain(domain_name, modified_xml)

        except Exception as exc:
            self.logger.error(f"failed to configure cpu/memory for {domain_name}: {exc}")
            return False

    def redefine_domain(self, domain_name: str, xml_content: str) -> bool:
        """
        Redefine a domain with new xml content.

        args:
            domain_name: name of the domain
            xml_content: new xml content for the domain

        returns:
            true if successful, false otherwise
        """
        try:
            # write xml to temporary file
            import os
            import tempfile

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as tmp_file:
                tmp_file.write(xml_content)
                tmp_file_path = tmp_file.name

            try:
                # define the domain from the xml file
                self.virsh.execute('define', tmp_file_path)
                self.logger.info(f"successfully redefined domain {domain_name}")
                return True

            finally:
                # clean up temporary file
                if os.path.exists(tmp_file_path):
                    os.unlink(tmp_file_path)

        except Exception as exc:
            self.logger.error(f"failed to redefine domain {domain_name}: {exc}")
            return False

    def hot_set_vcpus(self, domain_name: str, vcpu_count: int) -> bool:
        """
        Set the number of active vCPUs on a running domain.

        Uses virsh setvcpus with --live --config so the change applies
        immediately and persists across reboots.

        The domain's maximum vCPU count (//vcpu in XML) must already be
        >= vcpu_count, otherwise this will fail.

        Args:
            domain_name: name of the domain
            vcpu_count: desired number of active vCPUs

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self.virsh.execute(
                'setvcpus', domain_name, str(vcpu_count),
                '--live', '--config', warn=True)
            if not result.ok:
                self.logger.error(
                    f"failed to hot-set vCPUs for {domain_name}: {result.stderr}")
                return False
            self.logger.info(
                f"hot-set vCPUs to {vcpu_count} for {domain_name}")
            return True
        except Exception as exc:
            self.logger.error(f"failed to hot-set vCPUs for {domain_name}: {exc}")
            return False

    def hot_set_memory(self, domain_name: str, memory_mb: int) -> bool:
        """
        Set the active memory on a running domain.

        Uses virsh setmem with --live --config so the change applies
        immediately and persists across reboots.

        The domain's maximum memory (//memory in XML) must already be
        >= the target, otherwise this will fail.

        Args:
            domain_name: name of the domain
            memory_mb: desired memory in MiB

        Returns:
            True if successful, False otherwise
        """
        try:
            memory_kib = memory_mb * 1024
            result = self.virsh.execute(
                'setmem', domain_name, str(memory_kib),
                '--live', '--config', warn=True)
            if not result.ok:
                self.logger.error(
                    f"failed to hot-set memory for {domain_name}: {result.stderr}")
                return False
            self.logger.info(
                f"hot-set memory to {memory_mb}M for {domain_name}")
            return True
        except Exception as exc:
            self.logger.error(f"failed to hot-set memory for {domain_name}: {exc}")
            return False

