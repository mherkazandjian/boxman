import os
import json

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

        #: str: the path to the images cache directory
        self.images_cache_dir = os.path.join(self.cache_dir, 'images')

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

        if not os.path.exists(self.images_cache_dir):
            os.makedirs(self.images_cache_dir, exist_ok=True)

    def read_projects_cache(self) -> dict:
        """
        Read the cache and return its contents.
        """
        if not os.path.exists(self.projects_cache_file):
            log.warning(f"no projects cache file found at {self.projects_cache_file}")
            return {}

        with open(self.projects_cache_file, 'r') as fobj:
            self.projects = json.load(fobj)

        return self.projects

    def write_projects_cache(self) -> None:
        """
        Write the projects cache to the file.
        """
        if self.projects is None:
            log.warning("no projects to write to cache")
            return

        with open(self.projects_cache_file, 'w') as fobj:
            json.dump(self.projects, fobj, indent=4)
            log.info(f"projects cache written to {self.projects_cache_file}")


    def register_project(self,
                         project_name: str,
                         config_fpath: str) -> bool:
        """
        Register a project in the cache.

            <BOXMAN_CACHE_ROOT>/projects.json

        Args:
            project_name: Name of the project
            project_data: Data associated with the project
        """
        # if the projects.json file does not exist, create it else load it
        # assuming that it is a valid JSON file
        if not os.path.exists(self.projects_cache_file):
            with open(self.projects_cache_file, 'w') as fobj:
                self.projects = {}
                fobj.write('{}')
        else:
            self.read_projects_cache()

        # if the project already exists, log an error and return
        if project_name in self.projects:
            msg = f"Project '{project_name}' is already in the cache. Deprovision it first."
            log.error(msg)
            return False

        self.projects[project_name] = {
            'conf': os.path.abspath(os.path.expanduser(config_fpath))
        }

        with open(self.projects_cache_file, 'w') as fobj:
            json.dump(self.projects, fobj, indent=4)

        log.info(f"project '{project_name}' registered in cache with path: {config_fpath}")

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
        with open(self.projects_cache_file, 'w') as fobj:
            json.dump(self.projects, fobj, indent=4)

        log.info(f"project '{project_name}' unregistered from cache (was at: {removed_path})")
        return True
