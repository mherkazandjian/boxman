import json
import os
import tempfile

from boxman import log

DEFAULT_CACHE_DIR = '~/.config/boxman/cache'

class BoxmanCache:
    """
    The boxman cache manager

    The following directories and locations are used:

      - the cache directory: ~/.config/boxman/cache
      - the projects cache for projects that are managed by boxman:
           ~/.config/boxman/cache/projects.json
    """
    def __init__(self):
        """
        Initialize the boxman cache handler object
        """

        #: str: the path to the cache directory where boxman stores its data
        self.cache_dir = os.path.expanduser(DEFAULT_CACHE_DIR)

        #: str: the path to the projects cache file
        self.projects_cache_file = os.path.join(self.cache_dir, 'projects.json')

        #: dict: contains information about projects that are managed by boxman
        self.projects = None

        self.create_dir()

    def create_dir(self):
        """
        Create the cache directory if it does not exist
        """

        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir, exist_ok=True)
            log.info(f"cache directory created at: {self.cache_dir}")

    def _load_projects_cache(self) -> dict:
        """
        Read the projects cache file, tolerating a missing or corrupt file.

        A corrupt cache (e.g. truncated by a crashed writer) is moved aside
        and treated as empty instead of raising ``JSONDecodeError`` through
        every boxman command.
        """
        try:
            with open(self.projects_cache_file) as fobj:
                return json.load(fobj)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            backup = self.projects_cache_file + '.corrupt'
            os.replace(self.projects_cache_file, backup)
            log.warning(
                f"projects cache at {self.projects_cache_file} is corrupt; "
                f"moved aside to {backup} and treated as empty")
            return {}

    def _write_projects_cache_file(self) -> None:
        """
        Write the projects cache atomically.

        Dumps to a temporary file in the same directory and ``os.replace``es
        it into place, so a crashed or concurrent writer can never leave a
        truncated projects.json behind.
        """
        fd, tmp_file = tempfile.mkstemp(
            dir=self.cache_dir, prefix='.projects.json.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as fobj:
                json.dump(self.projects, fobj, indent=4)
            os.replace(tmp_file, self.projects_cache_file)
        except BaseException:
            # don't leave the staged file behind when the dump failed
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
            raise

    def read_projects_cache(self) -> dict:
        """
        Read the cache and return its contents.
        """
        if not os.path.exists(self.projects_cache_file):
            log.warning(f"no projects cache file found at {self.projects_cache_file}")
            return {}

        self.projects = self._load_projects_cache()

        return self.projects

    def write_projects_cache(self) -> None:
        """
        Write the projects cache to the file.
        """
        if self.projects is None:
            log.warning("no projects to write to cache")
            return

        self._write_projects_cache_file()
        log.info(f"projects cache written to {self.projects_cache_file}")


    def register_project(self,
                         project_name: str,
                         config_fpath: str,
                         runtime: str = 'local') -> bool:
        """
        Register a project in the cache.

            <BOXMAN_CACHE_ROOT>/projects.json

        Args:
            project_name: Name of the project
            config_fpath: Path to the project configuration file
            runtime: The runtime environment name (e.g. 'local', 'docker-compose')

        Returns:
            True if the project was registered, False if it was already
            in the cache.
        """
        self.projects = self._load_projects_cache()

        # if the project already exists, log an error and return
        if project_name in self.projects:
            msg = f"Project '{project_name}' is already in the cache. Deprovision it first."
            log.error(msg)
            return False

        self.projects[project_name] = {
            'conf': os.path.abspath(os.path.expanduser(config_fpath)),
            'runtime': runtime,
        }

        self._write_projects_cache_file()

        log.info(f"project '{project_name}' registered in cache with path: {config_fpath}, runtime: {runtime}")
        return True

    def unregister_project(self, project_name: str) -> bool:
        """
        Unregister a project from the cache.

        Args:
            project_name: Name of the project to unregister

        Returns:
            True if the project was unregistered, False otherwise
        """
        # load the projects file if it exists
        self.read_projects_cache()

        # check if the project exists in the cache
        if project_name not in self.projects:
            log.warning(f"project '{project_name}' is not in the cache, nothing to unregister")
            return False

        # remove the project and update the file
        removed_path = self.projects.pop(project_name)
        self._write_projects_cache_file()

        log.info(f"project '{project_name}' unregistered from cache (was at: {removed_path})")
        return True

    def unregister_network(self, project_name: str, network_name: str) -> bool:
        """
        Remove a network's entry from its project's cache record.

        Networks are added to the cache when defined; removing the libvirt
        network without dropping its entry would leave a stale record that
        ``check_network_exists()`` counts as a conflict with any later
        network of the same name.

        Args:
            project_name: Name of the project that owns the network
            network_name: Full name of the network to forget

        Returns:
            True if an entry was removed, False when the project or the
            network was not in the cache.
        """
        self.projects = self._load_projects_cache()
        project = (self.projects or {}).get(project_name)
        if not project or network_name not in project.get('networks', {}):
            return False

        del project['networks'][network_name]
        self._write_projects_cache_file()

        log.info(f"network '{network_name}' removed from the projects cache")
        return True

    def list_projects(self) -> dict:
        """
        List all registered projects.

        Returns:
            A dictionary of project names to their cached info,
            or an empty dict if no projects are registered.
        """
        return self.read_projects_cache()
