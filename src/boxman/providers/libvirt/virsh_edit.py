from lxml import etree, html
from typing import Dict, Any, Optional, List, Tuple
from boxman import log
from .commands import VirshCommand


class VirshEdit:
    """
    Class to edit libvirt domain XML using XPath expressions.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the VirshEdit class.

        Args:
            provider_config: Configuration for the libvirt provider
        """
        #: VirshCommand: the command executor for virsh
        self.virsh = VirshCommand(provider_config)

        #: logging.Logger: the logger instance
        self.logger = log

    def get_domain_xml(self, domain_name: str) -> str:
        """
        Get the XML definition of a domain.

        Args:
            domain_name: Name of the domain

        Returns:
            XML string of the domain definition
        """
        try:
            result = self.virsh.execute('dumpxml', domain_name)
            return result.stdout
        except Exception as exc:
            self.logger.error(f"Failed to get XML for domain {domain_name}: {exc}")
            raise

    def modify_xml_xpath(self,
                        xml_content: str,
                        modifications: List[Tuple[str, str, str]]) -> str:
        """
        Modify XML content using XPath expressions.

        Args:
            xml_content: The XML content to modify
            modifications: List of tuples (xpath, attribute_or_text, new_value)
                          Use 'text' for element text content
                          Use attribute name for attribute values

        Returns:
            Modified XML content as string
        """
        try:
            # Parse XML using lxml for better XPath support
            tree = etree.fromstring(xml_content.encode('utf-8'))

            for xpath, attr_or_text, new_value in modifications:
                # Use lxml XPath to find elements
                elements = tree.xpath(xpath)

                if not elements:
                    # Special handling for CPU topology - create if it doesn't exist
                    if xpath == '//cpu/topology':
                        cpu_elements = tree.xpath('//cpu')
                        if cpu_elements:
                            cpu_element = cpu_elements[0]
                            # Create topology element using XPath-style approach
                            topology_element = etree.Element('topology')
                            cpu_element.append(topology_element)
                            elements = [topology_element]
                            self.logger.info(f"Created new topology element under cpu")
                        else:
                            self.logger.warning(f"No cpu element found to add topology to")
                            continue
                    else:
                        self.logger.warning(f"No elements found for XPath: {xpath}")
                        continue
                for element in elements:
                    if attr_or_text == 'text':
                        element.text = new_value
                    else:
                        element.set(attr_or_text, new_value)

                self.logger.debug(f"Modified {len(elements)} element(s) at {xpath}")

            # Return the modified XML as string
            return etree.tostring(tree, encoding='unicode', pretty_print=True)

        except etree.XMLSyntaxError as exc:
            self.logger.error(f"Failed to parse XML: {exc}")
            raise
        except Exception as exc:
            self.logger.error(f"Failed to modify XML: {exc}")
            raise

    def find_xpath_values(self, xml_content: str, xpath: str) -> List[str]:
        """
        Find values using XPath expressions.

        Args:
            xml_content: The XML content to search
            xpath: XPath expression to find values

        Returns:
            List of matching values
        """
        try:
            tree = etree.fromstring(xml_content.encode('utf-8'))
            matches = tree.xpath(xpath)

            # Convert results to strings
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
            self.logger.error(f"Failed to parse XML: {exc}")
            raise
        except Exception as exc:
            self.logger.error(f"Failed to find XPath values: {exc}")
            raise

    def configure_cpu_memory(self,
                           domain_name: str,
                           cpus: Optional[Dict[str, int]] = None,
                           memory_mb: Optional[int] = None) -> bool:
        """
        Configure CPU and memory settings for a domain.

        Args:
            domain_name: Name of the domain
            cpus: Dictionary with 'sockets', 'cores', 'threads' keys
            memory_mb: Memory in MB

        Returns:
            True if successful, False otherwise
        """
        try:
            # Get current XML
            xml_content = self.get_domain_xml(domain_name)

            # Debug: Log current CPU configuration
            tree = etree.fromstring(xml_content.encode('utf-8'))
            cpu_elements = tree.xpath('//cpu')
            if cpu_elements:
                self.logger.debug(f"Found CPU element: {etree.tostring(cpu_elements[0], encoding='unicode')}")

            topology_elements = tree.xpath('//cpu/topology')
            if topology_elements:
                self.logger.debug(f"Found topology element: {etree.tostring(topology_elements[0], encoding='unicode')}")
            else:
                self.logger.debug("No topology element found")

            modifications = []

            # Configure memory if specified
            if memory_mb is not None:
                memory_kb = memory_mb * 1024
                modifications.extend([
                    ('//memory', 'text', str(memory_kb)),
                    ('//currentMemory', 'text', str(memory_kb))
                ])

            # Configure CPU if specified
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
                self.logger.info(f"No CPU/memory modifications needed for {domain_name}")
                return True

            # Apply modifications
            modified_xml = self.modify_xml_xpath(xml_content, modifications)

            # Redefine the domain
            return self.redefine_domain(domain_name, modified_xml)

        except Exception as exc:
            self.logger.error(f"Failed to configure CPU/memory for {domain_name}: {exc}")
            return False

    def redefine_domain(self, domain_name: str, xml_content: str) -> bool:
        """
        Redefine a domain with new XML content.

        Args:
            domain_name: Name of the domain
            xml_content: New XML content for the domain

        Returns:
            True if successful, False otherwise
        """
        try:
            # Write XML to temporary file
            import tempfile
            import os

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as tmp_file:
                tmp_file.write(xml_content)
                tmp_file_path = tmp_file.name

            try:
                # Define the domain from the XML file
                self.virsh.execute('define', tmp_file_path)
                self.logger.info(f"Successfully redefined domain {domain_name}")
                return True

            finally:
                # Clean up temporary file
                if os.path.exists(tmp_file_path):
                    os.unlink(tmp_file_path)

        except Exception as exc:
            self.logger.error(f"Failed to redefine domain {domain_name}: {exc}")
            return False
