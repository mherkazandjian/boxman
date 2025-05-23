import xml.etree.ElementTree as ET
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
            root = ET.fromstring(xml_content)

            for xpath, attr_or_text, new_value in modifications:
                elements = root.findall(xpath)

                if not elements:
                    self.logger.warning(f"No elements found for XPath: {xpath}")
                    continue

                for element in elements:
                    if attr_or_text == 'text':
                        element.text = new_value
                    else:
                        element.set(attr_or_text, new_value)

                self.logger.debug(f"Modified {len(elements)} element(s) at {xpath}")

            return ET.tostring(root, encoding='unicode')

        except ET.ParseError as exc:
            self.logger.error(f"Failed to parse XML: {exc}")
            raise
        except Exception as exc:
            self.logger.error(f"Failed to modify XML: {exc}")
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
            modifications = []

            # Configure memory if specified
            if memory_mb is not None:
                memory_kb = memory_mb * 1024
                modifications.extend([
                    ('./memory', 'text', str(memory_kb)),
                    ('./currentMemory', 'text', str(memory_kb))
                ])

            # Configure CPU if specified
            if cpus:
                sockets = cpus.get('sockets', 1)
                cores = cpus.get('cores', 1)
                threads = cpus.get('threads', 1)
                total_vcpus = sockets * cores * threads

                modifications.extend([
                    ('./vcpu', 'text', str(total_vcpus)),
                    ('./cpu/topology', 'sockets', str(sockets)),
                    ('./cpu/topology', 'cores', str(cores)),
                    ('./cpu/topology', 'threads', str(threads))
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
