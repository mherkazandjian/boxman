import yaml
from typing import Dict, Any, Optional, Union


class BoxmanManager:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the BoxmanManager.

        Args:
            config: Optional configuration dictionary or path to config file
        """
        self.config_path: Optional[str] = None
        self.config: Optional[Dict[str, Any]] = None

        if isinstance(config, str):
            self.config_path = config
            self.config = self.load_config(config)

    def load_config(self, config_path: str) -> Dict[str, Any]:
        """
        Load configuration from a YAML file.

        Args:
            config_path: Path to the configuration file

        Returns:
            Dict containing the configuration
        """
        # Load the configuration file
        with open(config_path) as fobj:
            conf: Dict[str, Any] = yaml.safe_load(fobj.read())
        return conf
