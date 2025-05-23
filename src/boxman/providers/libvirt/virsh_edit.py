from lxml import etree, html
from typing import Dict, Any, Optional, List, Tuple
from boxman import log
from .commands import VirshCommand


class VirshEdit:
    """
    Class to edit libvirt domain xml using xpath expressions.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        initialize the virshedit class.

        args:
            provider_config: configuration for the libvirt provider
        """
        #: VirshCommand: the command executor for virsh
        self.virsh = VirshCommand(provider_config)

        #: logging.Logger: the logger instance
        self.logger = log

    def get_domain_xml(self, domain_name: str) -> str:
        """
        Get the xml definition of a domain.

        args:
            domain_name: name of the domain

        returns:
            xml string of the domain definition
        """
        try:
            result = self.virsh.execute('dumpxml', domain_name)
            return result.stdout
        except Exception as exc:
            self.logger.error(f"failed to get xml for domain {domain_name}: {exc}")
            raise

    def modify_xml_xpath(self,
                        xml_content: str,
                        modifications: List[Tuple[str, str, str]]) -> str:
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
                            self.logger.info(f"created new topology element under cpu")
                        else:
                            self.logger.warning(f"no cpu element found to add topology to")
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

    def find_xpath_values(self, xml_content: str, xpath: str) -> List[str]:
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

    def configure_cpu_memory(self,
                           domain_name: str,
                           cpus: Optional[Dict[str, int]] = None,
                           memory_mb: Optional[int] = None) -> bool:
        """
        Configure cpu and memory settings for a domain.

        args:
            domain_name: name of the domain
            cpus: dictionary with 'sockets', 'cores', 'threads' keys
            memory_mb: memory in mb

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

            modifications = []

            # configure memory if specified
            if memory_mb is not None:
                memory_kb = memory_mb * 1024
                modifications.extend([
                    ('//memory', 'text', str(memory_kb)),
                    ('//currentMemory', 'text', str(memory_kb))
                ])

            # configure cpu if specified
            if cpus:
                sockets = cpus.get('sockets', 1)
                cores = cpus.get('cores', 1)
                threads = cpus.get('threads', 1)
                total_vcpus = sockets * cores * threads

                modifications.extend([
                    ('//vcpu', 'text', str(total_vcpus)),
                    ('//cpu/topology', 'sockets', str(sockets)),
                    ('//cpu/topology', 'cores', str(cores)),
                    ('//cpu/topology', 'threads', str(threads))
                ])

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
            import tempfile
            import os

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
