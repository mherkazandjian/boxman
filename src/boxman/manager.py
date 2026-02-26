import os
import time
import re
from typing import Dict, Any, Optional
import yaml
from multiprocessing import Pool
from multiprocessing import Process
from invoke import run
from jinja2 import Environment, FileSystemLoader, Template

from boxman.providers.libvirt.session import LibVirtSession
from boxman.config_cache import BoxmanCache
from boxman.utils.io import write_files
from boxman import log
from boxman.runtime import create_runtime, RuntimeBase
from boxman.utils.jinja_env import create_jinja_env
from boxman.providers.libvirt.commands import VirshCommand

class BoxmanManager:
    def __init__(self,
                 config: Optional[Dict[str, Any]] = None):
        """
        Initialize the BoxmanManager.

        Args:
            config: Optional configuration dictionary or path to config file
        """
        #: Optional[str]: the path to the configuration file if one was provided
        self.config_path: Optional[str] = None

        #: Optional[Dict[str, Any]]: the loaded configuration dictionary
        self.config: Optional[Dict[str, Any]] = None

        #: the private backing field for the provider property
        self._provider = None

        #: the logger instance
        self.logger = log

        #: str: the runtime environment name ('local', 'docker-compose', etc.)
        self._runtime_name: str = 'local'

        #: Optional[RuntimeBase]: the resolved runtime instance (created lazily)
        self._runtime_instance: Optional[RuntimeBase] = None

        if isinstance(config, str):
            self.config_path = config
            self.config = self.load_config(config)

        self.cache = BoxmanCache()

        #: Optional[Dict[str, Any]]: the boxman application-level config (from boxman.yml)
        self.app_config: Optional[Dict[str, Any]] = None

    def load_app_config(self, config: Dict[str, Any]) -> None:
        """
        Load the boxman application-level configuration (from boxman.yml).

        Args:
            config: The parsed boxman.yml configuration dictionary
        """
        self.app_config = config

    @property
    def provider(self) -> Optional["LibVirtSession"]:
        """
        Get the current provider session.

        Returns:
            The provider session instance or None if not initialized
        """
        return self._provider

    @provider.setter
    def provider(self, value: "LibVirtSession") -> None:
        """
        Set the provider session.

        Args:
            value: The provider session instance
        """
        self._provider = value

    @property
    def runtime(self) -> str:
        """Return the runtime environment name."""
        return self._runtime_name

    @runtime.setter
    def runtime(self, value: str) -> None:
        """Set the runtime environment name and reset the cached instance."""
        self._runtime_name = value
        self._runtime_instance = None  # force re-creation

    @property
    def runtime_instance(self) -> RuntimeBase:
        """
        Return the runtime instance, creating it on first access.

        The runtime config is taken from ``app_config`` if available.
        """
        if self._runtime_instance is None:
            runtime_config = (self.app_config or {}).get("runtime_config", {})
            self._runtime_instance = create_runtime(
                self._runtime_name, config=runtime_config
            )
        return self._runtime_instance

    def get_provider_config_with_runtime(
        self, provider_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Return a copy of *provider_config* enriched with runtime metadata.

        This should be called before passing the config to provider command
        classes (``VirshCommand``, ``VirtInstallCommand``, etc.) so they
        know how to wrap commands.

        Args:
            provider_config: The raw provider configuration dict.

        Returns:
            A new dict with ``runtime`` and related keys injected.
        """
        return self.runtime_instance.inject_into_provider_config(provider_config)

    def load_config(self, config_path: str) -> Dict[str, Any]:
        """
        Load configuration from a YAML file.

        The file is first treated as a Jinja template and rendered,
        then parsed as YAML.

        Args:
            config_path: Path to the configuration file

        Returns:
            Dict containing the configuration
        """
        # get the directory and filename for jinja template loading
        config_dir = os.path.dirname(os.path.abspath(config_path))
        config_filename = os.path.basename(config_path)

        # create jinja environment with boxman helpers (env(), env_required(), etc.)
        env = create_jinja_env(config_dir)

        # load the template
        template = env.get_template(config_filename)

        # render the template
        # NOTE: pass os.environ as 'environ' (not 'env') to avoid shadowing
        # the env() helper function registered in the Jinja globals.
        rendered_yaml = template.render(
            environ=os.environ,
        )

        # parse the rendered yaml
        conf = yaml.safe_load(rendered_yaml)

        # dump the rendered yaml file for debugging/inspection
        rendered_filename = f"{os.path.splitext(config_filename)[0]}.rendered.yml"
        rendered_path = os.path.join(config_dir, rendered_filename)
        with open(rendered_path, 'w') as fobj:
            fobj.write(rendered_yaml)
            self.logger.info(f"rendered YAML template written to {rendered_path}")

        return conf

    def provision_files(self) -> None:
        """
        Provision files specified in the cluster configuration.
        """
        clusters = self.config['clusters']
        for cluster_name, cluster in clusters.items():
            if files := cluster.get('files'):
                write_files(files, rootdir=cluster['workdir'])

    @classmethod
    def full_network_name(cls,
                          project_config: Dict[str, Any],
                          cluster_name: str = None,
                          network_name: str = None) -> str:
        """
        Return the computed network name based on how it is resolved in the name string.

        The network name is expected to have the following format:

            <project_name>::<cluster_name>::<base_network_name>

        The project prefix is always added to the network name.

           prj_name = f'bprj__{project_config["project"]}__bprj'

        The cluster name is added after the project name as such

           cluster_name = f'clstr__{cluster_name}__clstr'

        Finally the network name is added as such

            full_network_name = f'{prj_name}__{cluster_name}__{base_network_name}'

        Args:
            project_config (Dict[str, Any]): The project configuration dictionary.
            network_name (str): The network name string, e.g. 'base_name',
              'cluster_name::base_name', or 'project_name::cluster_name::base_name'.

        Returns:
            str: The fully qualified network name.
        """
        parts = network_name.split("::")

        if len(parts) == 3:           # project, cluster, base
            _project, _cluster_name, _base_name = parts
            retval = f'bprj__{_project}__bprj'
            retval = retval + f'__clstr__{_cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        elif len(parts) == 2:         # cluster, base
            _cluster_name, _base_name = parts
            retval = f'bprj__{project_config["project"]}__bprj'
            retval = retval + f'__clstr__{_cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        elif len(parts) == 1:         # base only
            _base_name = parts[0]
            retval = f'bprj__{project_config["project"]}__bprj'
            retval = retval + f'__clstr__{cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        else:
            raise ValueError(f"Invalid network name format: {network_name}")

    @staticmethod
    def import_image(cls, cli_args) -> None:
        """
        Import an image into the provider's storage.

        :param manager: The instance of the BoxmanManager
        :param cli_args: The parsed arguments from the cli
        """
        cls.provider.import_image(
            manifest_uri=cli_args.manifest_uri,
            vm_name=cli_args.vm_name,
            vm_dir=cli_args.vm_dir
        )

    @staticmethod
    def create_templates(cls, cli_args) -> None:
        """
        Create template VMs from cloud images using cloud-init.

        Reads the ``templates`` section from the project config and creates
        each template VM that doesn't already exist.

        :param cls: The BoxmanManager instance
        :param cli_args: The parsed arguments from the cli
        """
        requested = None
        if cli_args is not None and hasattr(cli_args, 'template_names') and cli_args.template_names:
            requested = [t.strip() for t in cli_args.template_names.split(',')]

        force = getattr(cli_args, 'force', False) if cli_args is not None else False

        cls._create_templates_impl(requested=requested, force=force)

    def _create_templates_impl(self, requested=None, force=False) -> None:
        """
        Internal implementation for creating template VMs.

        Args:
            requested: Optional list of template keys to create (None = all).
            force: If True, recreate existing templates.
        """
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate

        config = self.config
        templates = config.get('templates', {})

        if not templates:
            self.logger.warning("no templates defined in configuration")
            return

        # determine provider config
        provider_type = list(config.get('provider', {}).keys())[0] if 'provider' in config else 'libvirt'
        provider_config = config.get('provider', {}).get(provider_type, {})

        # merge app-level provider config as defaults
        if self.app_config and 'providers' in self.app_config:
            app_provider = self.app_config['providers'].get(provider_type, {})
            merged = app_provider.copy()
            merged.update(provider_config)
            provider_config = merged

        # inject runtime settings
        if hasattr(self, 'runtime_instance'):
            provider_config = self.runtime_instance.inject_into_provider_config(provider_config)

        self.logger.info(f"resolved provider config for templates: {provider_config}")

        # resolve a workdir for template artifacts
        default_workdir = '~/boxman-templates'
        for _, cluster in config.get('clusters', {}).items():
            default_workdir = cluster.get('workdir', default_workdir)
            break

        # --- Pre-check: detect already-existing templates ----------------
        # Build a temporary VirshCommand to query existing VMs once.
        from boxman.providers.libvirt.commands import VirshCommand
        _virsh = VirshCommand(provider_config=provider_config)
        _existing_vms: set = set()
        result = _virsh.execute("list", "--all", "--name", hide=True, warn=True)
        if result.ok:
            _existing_vms = {
                v.strip() for v in result.stdout.strip().split("\n") if v.strip()
            }

        # Identify which of the requested templates already exist
        existing_templates: list[str] = []
        templates_to_create: list[str] = []

        for tpl_key, tpl_conf in templates.items():
            if requested and tpl_key not in requested:
                continue
            tpl_name = tpl_conf.get('name', tpl_key)
            if tpl_name in _existing_vms:
                existing_templates.append(tpl_key)
            else:
                templates_to_create.append(tpl_key)

        # If any templates already exist and --force was NOT given, error out.
        if existing_templates and not force:
            names = ", ".join(
                f"'{templates[k].get('name', k)}'" for k in existing_templates
            )
            self.logger.error(
                f"the following template(s) already exist: {names}. "
                f"Use --force to delete and recreate them."
            )
            return

        # Merge both lists (existing ones will be force-recreated)
        all_keys = existing_templates + templates_to_create
        # -----------------------------------------------------------------

        for tpl_key in all_keys:
            tpl_conf = templates[tpl_key]

            tpl_name = tpl_conf.get('name', tpl_key)
            image_path = tpl_conf.get('image', '')
            cloudinit_userdata = tpl_conf.get('cloudinit', None)
            cloudinit_metadata = tpl_conf.get('cloudinit_metadata', None)
            cloudinit_network_config = tpl_conf.get('cloudinit_network_config', None)
            tpl_memory = tpl_conf.get('memory', 2048)
            tpl_vcpus = tpl_conf.get('vcpus', 2)
            tpl_os_variant = tpl_conf.get('os_variant', 'generic')
            tpl_disk_format = tpl_conf.get('disk_format', 'qcow2')
            tpl_network = tpl_conf.get('network', 'default')
            tpl_bridge = tpl_conf.get('bridge', None)
            tpl_workdir = tpl_conf.get('workdir', default_workdir)

            # Ensure the workdir exists and is writable by the current user.
            # Earlier steps (e.g. docker runtime) may have created it as root.
            expanded_workdir = os.path.expanduser(tpl_workdir)
            self._ensure_writable_dir(expanded_workdir)

            # Also pre-create the template subdirectory that cloudinit.py
            # will use, so it doesn't hit PermissionError.
            template_subdir = os.path.join(expanded_workdir, tpl_name)
            self._ensure_writable_dir(template_subdir)

            self.logger.info(f"creating template '{tpl_key}' -> VM name '{tpl_name}'")

            ct = CloudInitTemplate(
                template_name=tpl_name,
                image_path=image_path,
                cloudinit_userdata=cloudinit_userdata,
                cloudinit_metadata=cloudinit_metadata,
                cloudinit_network_config=cloudinit_network_config,
                workdir=tpl_workdir,
                provider_config=provider_config,
                memory=tpl_memory,
                vcpus=tpl_vcpus,
                os_variant=tpl_os_variant,
                disk_format=tpl_disk_format,
                network=tpl_network,
                bridge=tpl_bridge,
            )

            success = ct.create_template(force=force)
            if success:
                self.logger.info(f"template '{tpl_key}' created successfully")
            else:
                self.logger.error(f"failed to create template '{tpl_key}'")

    def _ensure_writable_dir(self, path: str) -> None:
        """
        Ensure *path* exists and is writable by the current user.

        If the directory was created by another user (e.g. root via docker),
        attempt to fix ownership with ``sudo chown``.  If ``sudo`` is not
        available or fails, a clear error message is logged.

        When running under a non-local runtime the directory is also
        created inside the container so that commands executed via
        ``docker exec`` can access it.

        Args:
            path: Absolute or user-expandable directory path.
        """
        path = os.path.expanduser(path)

        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except PermissionError:
                # parent dir may be owned by root — try sudo mkdir
                self.logger.warning(
                    f"cannot create '{path}' as current user, "
                    f"trying with sudo...")
                result = run(
                    f"sudo mkdir -p '{path}'", hide=True, warn=True)
                if not result.ok:
                    raise PermissionError(
                        f"failed to create directory '{path}' even with sudo"
                    )
                # fall through to chown below

        # if the directory exists but is not writable, fix ownership
        if not os.access(path, os.W_OK):
            uid = os.getuid()
            gid = os.getgid()
            self.logger.info(
                f"fixing ownership of '{path}' to {uid}:{gid} "
                f"(was not writable by current user)")
            result = run(
                f"sudo chown -R {uid}:{gid} '{path}'",
                hide=True, warn=True)
            if not result.ok:
                raise PermissionError(
                    f"directory '{path}' is not writable and "
                    f"'sudo chown' failed: {result.stderr.strip()}"
                )

        # When using a non-local runtime (e.g. docker-compose), also
        # create the directory inside the container so that commands
        # executed via 'docker exec' can write to it.
        if self._runtime_name != 'local':
            mkdir_cmd = self.runtime_instance.wrap_command(
                f"mkdir -p '{path}'"
            )
            self.logger.info(f"creating directory inside runtime container: {path}")
            result = run(mkdir_cmd, hide=True, warn=True)
            if not result.ok:
                self.logger.warning(
                    f"failed to create '{path}' inside container: "
                    f"{result.stderr.strip()}")

    def ensure_templates_exist(self) -> bool:
        """
        Check if any cluster's base_image refers to a template defined in the
        ``templates`` section of the config. If the template VM does not exist,
        create it automatically.

        Returns:
            True if all required templates exist (or were created), False on failure.
        """
        templates = self.config.get('templates', {})
        if not templates:
            return True  # nothing to do

        # build a mapping: template VM name -> template key
        tpl_name_to_key: Dict[str, str] = {}
        for tpl_key, tpl_conf in templates.items():
            vm_name = tpl_conf.get('name', tpl_key)
            tpl_name_to_key[vm_name] = tpl_key
            # also map by the key itself in case base_image uses the key
            tpl_name_to_key[tpl_key] = tpl_key

        # collect which templates are referenced by clusters
        needed_template_keys: set = set()
        for cluster_name, cluster in self.config.get('clusters', {}).items():
            base_image = cluster.get('base_image', '')
            if base_image in tpl_name_to_key:
                needed_template_keys.add(tpl_name_to_key[base_image])

        if not needed_template_keys:
            return True  # no cluster uses a template as base_image

        # check which of the needed templates already exist as VMs
        missing_keys: list = []
        for tpl_key in needed_template_keys:
            tpl_conf = templates[tpl_key]
            tpl_vm_name = tpl_conf.get('name', tpl_key)

            # ask the provider if the VM exists
            exists = False
            if self.provider is not None and hasattr(self.provider, 'vm_exists'):
                exists = self.provider.vm_exists(tpl_vm_name)
            elif self.provider is not None:
                # fallback: try virsh list
                try:
                    from boxman.providers.libvirt.commands import VirshCommand
                    provider_type = list(self.config.get('provider', {}).keys())[0]
                    provider_config = self.config.get('provider', {}).get(provider_type, {})
                    if self.app_config and 'providers' in self.app_config:
                        app_prov = self.app_config['providers'].get(provider_type, {})
                        merged = app_prov.copy()
                        merged.update(provider_config)
                        provider_config = merged
                    if hasattr(self, 'runtime_instance'):
                        provider_config = self.runtime_instance.inject_into_provider_config(provider_config)
                    virsh = VirshCommand(provider_config=provider_config)
                    result = virsh.execute("list", "--all", "--name", hide=True, warn=True)
                    if result.ok:
                        vm_list = [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]
                        exists = tpl_vm_name in vm_list
                except Exception as exc:
                    self.logger.warning(f"could not check if template VM '{tpl_vm_name}' exists: {exc}")

            if exists:
                self.logger.info(f"template VM '{tpl_vm_name}' already exists, skipping creation")
            else:
                self.logger.info(
                    f"template VM '{tpl_vm_name}' (key='{tpl_key}') does not exist, "
                    f"will create it before provisioning")
                missing_keys.append(tpl_key)

        if not missing_keys:
            return True

        # create the missing templates
        self.logger.info(f"auto-creating {len(missing_keys)} missing template(s): {missing_keys}")
        self._create_templates_impl(requested=missing_keys, force=False)

        return True

    ### register/un-register the project in the cache
    def register_project_in_cache(self) -> None:
        """
        Register the project in the Boxman cache.

        This method saves the project configuration to the cache for later use.

        Raises:
            RuntimeError: If the project is already registered in the cache.
        """
        success = self.cache.register_project(
            project_name=self.config['project'],
            config_fpath=self.config_path,
            runtime=self._runtime_name)

        if success is False:
            raise RuntimeError(
                f"Project '{self.config['project']}' is already in the cache. "
                f"Deprovision it first with: boxman deprovision"
            )

    def unregister_from_cache(self) -> None:
        """
        Register the project in the Boxman cache.

        This method saves the project configuration to the cache for later use.
        """
        self.cache.unregister_project(project_name=self.config['project'])

    @staticmethod
    def list_projects(cls, cli_args) -> None:
        """
        List all registered projects.
        """
        if hasattr(cls.cache, 'list_projects'):
            projects = cls.cache.list_projects()
            if not projects:
                cls.logger.info("No projects registered.")
            else:
                cls.logger.info("Registered projects:")
                if isinstance(projects, dict):
                    for proj_name, proj_info in projects.items():
                        cls.logger.info(f"  - {proj_name}: {proj_info}")
                else:
                    for proj in projects:
                        cls.logger.info(f"  - {proj}")
        else:
            cls.logger.error("BoxmanCache does not implement list_projects()")
    ### end register the project in the cache

    ### networks define / remove / destroy
    def define_networks(self) -> None:
        """
        Define the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in cluster.get('networks', {}).items():

                _network_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name
                )

                self.provider.define_network(
                    name=_network_name,
                    info=network_info,
                    workdir=cluster['workdir']
                )
                self.logger.info(f"defined network {_network_name} in {cluster['workdir']}")

    def destroy_networks(self) -> None:
        """
        Destroy the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in cluster.get('networks', {}).items():

                _network_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name
                )

                self.provider.remove_network(
                    name=_network_name,
                    info=network_info
                )
                self.logger.info(f"removed network {_network_name} in {cluster['workdir']}")
    ### end networks define / remove / destroy

    ### vms define / remove / destroy
    def clone_vms(self) -> None:
        """
        Clone the VMs defined in the configuration.

        The following is done for every vm in every cluster

            - remove the vm
            - clone the vm
        """
        def vm_clone_tasks():
            prj_name = f'bprj__{self.config["project"]}__bprj'
            for cluster_name, cluster in self.config['clusters'].items():
                for vm_name, vm_info in cluster['vms'].items():
                    vm_info = vm_info.copy()
                    new_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    yield cluster, vm_info, new_vm_name

        def _clone(cluster, vm_info, new_vm_name):
            self.provider.clone_vm(
                src_vm_name=cluster['base_image'],
                new_vm_name=new_vm_name,
                info=vm_info,
                workdir=cluster['workdir']
            )

        # clone the vms one at a time
        for cluster, vm_info, new_vm_name in vm_clone_tasks():
            self.logger.info(f"cloning vm {new_vm_name} from base image {cluster['base_image']}")
            _clone(cluster, vm_info, new_vm_name)
            time.sleep(1)  # Add a small delay to avoid overwhelming the provider

        # optionally use multiprocessing to speed up the cloning process
        #processes = [
        #    Process(target=_clone, args=(cluster, vm_info, new_vm_name))
        #    for cluster, vm_info, new_vm_name in vm_clone_tasks()
        #]
        #[p.start() for p in processes]
        #[p.join() for p in processes]

    def destroy_vms(self) -> None:
        """
        Destroy the VMs specified in the cluster configuration.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        def vm_destroy_tasks():
            for cluster_name, cluster in self.config['clusters'].items():
                for vm_name in cluster['vms'].keys():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    yield full_vm_name, cluster_name, vm_name

        def _destroy(full_vm_name, cluster_name, vm_name):
            self.logger.info(f"Destroying VM {vm_name} in cluster {cluster_name}")
            self.provider.destroy_vm(
                name=full_vm_name,
                remove_storage=True
            )

        processes = [
            Process(target=_destroy, args=(full_vm_name, cluster_name, vm_name))
            for full_vm_name, cluster_name, vm_name in vm_destroy_tasks()
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]
    ### end vms define / remove / destroy

    def configure_network_interfaces(self) -> None:
        """
        Configure network interfaces for all VMs based on their network_adapters configuration.

        This method adds network interfaces to VMs after they have been cloned,
        connecting them to the appropriate networks.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():

                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                vm_info = vm_info.copy()

                self.logger.info(
                    f"configuring network interfaces for VM {vm_name} in cluster {cluster_name}")

                if 'network_adapters' not in vm_info:
                    self.logger.warning(f"no network adapters defined for vm {vm_name}, skipping")
                    continue
                else:
                    # use the fully qualified network name to which the adapter will connect to
                    for adapter in vm_info['network_adapters']:

                        if adapter.get('is_global', False):
                            full_network_name = adapter['network_source']
                        else:
                            full_network_name = self.full_network_name(
                                project_config=self.config,
                                cluster_name=cluster_name,
                                network_name=adapter['network_source']
                            )

                        adapter['network_source'] = full_network_name

                success = self.provider.configure_vm_network_interfaces(
                    vm_name=full_vm_name,
                    network_adapters=vm_info['network_adapters']
                )

                if success:
                    self.logger.info(
                        f"All network interfaces configured successfully for vm {vm_name}")
                else:
                    self.logger.warning(
                        f"Some network interfaces could not be configured for vm {vm_name}")

    def configure_disks(self) -> None:
        """
        Configure disks for all VMs based on their disks configuration.

        This method creates and attaches disks to VMs after they have been cloned.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = cluster.get('workdir', '.')

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"configuring disks for vm {vm_name} in cluster {cluster_name}")

                # check if there are disks defined in the VM configuration
                if 'disks' not in vm_info or not vm_info['disks']:
                    self.logger.warning(f"no disks defined for vm {vm_name}, skipping")
                    continue

                # configure all disks for this vm
                success = self.provider.configure_vm_disks(
                    vm_name=full_vm_name,
                    disks=vm_info['disks'],
                    workdir=workdir,
                    disk_prefix=full_vm_name)

                if success:
                    self.logger.info(f"all disks configured successfully for vm {vm_name}")
                else:
                    self.logger.warning(f"some disks could not be configured for vm {vm_name}")

    def configure_cpu_mem(self) -> None:
        """
        Configure cpu and memory settings for all vms based on their configuration.

        This method modifies the CPU and memory settings of VMs after they have been cloned.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(
                    f"configuring cpu and memory for vm {vm_name} in cluster {cluster_name}")

                # extract the cpu and memory configuration
                cpus = vm_info.get('cpus')
                memory = vm_info.get('memory')

                # skip if neither cpu nor memory is configured
                if not cpus and not memory:
                    self.logger.warning(
                        f"no cpu or memory configuration for vm {vm_name}, skipping")
                    continue

                # configure cpu and memory
                success = self.provider.configure_vm_cpu_memory(
                    vm_name=full_vm_name,
                    cpus=cpus,
                    memory_mb=memory
                )

                if success:
                    self.logger.info(f"successfully configured cpu and memory for vm {vm_name}")
                else:
                    self.logger.warning(f"failed to configure cpu and memory for vm {vm_name}")

    def start_vms(self) -> None:
        """
        Start all VMs in the configuration.

        This powers on all VMs after they have been configured.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"starting vm {full_vm_name}")
                success = self.provider.start_vm(full_vm_name)

                if success:
                    self.logger.info(f"successfully started the vm {full_vm_name}")
                else:
                    self.logger.warning(f"failed to start the vm {full_vm_name}")

    def get_connect_info(self) -> bool:
        """
        Gather connection information for all VMs in all clusters.

        This method attempts to get IP addresses for all VMs and returns
        True only if all VMs have at least one IP address.

        Returns:
            True if all VMs have at least one IP address, False otherwise
        """
        all_vms_have_ip = True

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                # get the ip addresses for this vm
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                # if no ip addresses found, mark as failure
                if not ip_addresses:
                    all_vms_have_ip = False
                    self.logger.warning(f"vm {full_vm_name} does not have an ip address yet")

        return all_vms_have_ip

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        self.logger.info("=== vm connection information ===")

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            self.logger.info(f"cluster: {cluster_name}")
            self.logger.info("-" * 60)

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                hostname = vm_info.get('hostname', vm_name)

                self.logger.info(f"vm: {vm_name} (hostname: {hostname})")

                # get the ip addresses for all interfaces
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    self.logger.info("  ip addresses:")
                    for iface, ip in ip_addresses.items():
                        self.logger.info(f"    {iface}: {ip}")
                else:
                    self.logger.info("  ip addresses: not available")

                # get ssh connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                admin_key = os.path.expanduser(os.path.join(
                    cluster.get('workdir', '~'),
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                self.logger.info("  connect via ssh:")
                # show direct connection if ip is available
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    self.logger.info(f"    direct: ssh -i {admin_key} {admin_user}@{first_ip}")

                # show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        cluster.get('workdir', '~'),
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    self.logger.info(f"    via config: ssh -F {ssh_config} {hostname}")

                self.logger.info("")

            self.logger.info("")

    def write_ssh_config(self) -> None:
        """
        Generate SSH configuration file for easy access to VMs.

        Creates an SSH config file in the workdir of each cluster that allows
        simplified access to VMs without typing full connection details.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            # get the ssh config path
            ssh_config = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('ssh_config', 'ssh_config')
            ))

            admin_priv_key = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('admin_key_name', 'id_ed25519_boxman')
            ))

            self.logger.info(f"writing ssh config to {ssh_config}")

            with open(ssh_config, 'w') as fobj:
                # write global SSH options
                fobj.write('Host *\n')
                fobj.write('    StrictHostKeyChecking no\n')
                fobj.write('    UserKnownHostsFile /dev/null\n')
                fobj.write('\n\n')

                # write host-specific configurations
                for vm_name, vm_info in cluster['vms'].items():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    hostname = vm_info.get('hostname', vm_name)

                    # get the first ip address if available
                    ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                    if ip_addresses:
                        first_ip = next(iter(ip_addresses.values()))

                        fobj.write(f'Host {hostname}\n')
                        fobj.write(f'    Hostname {first_ip}\n')
                        fobj.write(f'    User {cluster.get("admin_user", "admin")}\n')
                        fobj.write(f'    IdentityFile {admin_priv_key}\n')
                        fobj.write('\n\n')
                    else:
                        self.logger.warning(
                            f"no ip address available for the vm {vm_name}, "
                            "skipping SSH config entry")

            self.logger.info(f"ssh config file written to {ssh_config}")
            self.logger.info(f"to connect: ssh -F {ssh_config} <hostname>")

    def generate_ssh_keys(self) -> bool:
        """
        Generate SSH keys for connecting to VMs.

        Creates an SSH key pair in each cluster's workdir if it doesn't already exist.

        Returns:
            bool: True if successful, False otherwise
        """
        success = True

        for _, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')

            admin_priv_key = os.path.join(workdir, admin_key_name)
            admin_pub_key = os.path.join(workdir, f"{admin_key_name}.pub")

            # create workdir if it doesn't exist
            if not os.path.isdir(workdir):
                os.makedirs(workdir, exist_ok=True)

            # generate key pair if it doesn't exist
            if not os.path.exists(admin_priv_key):
                self.logger.info(f"generating ssh key pair in {workdir}")

                try:
                    cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
                    result = run(cmd, hide=True, warn=True)

                    # verify keys were created
                    if os.path.isfile(admin_priv_key) and os.path.isfile(admin_pub_key):
                        self.logger.info(f"ssh key pair successfully generated at {admin_priv_key}")
                    else:
                        self.logger.warning(f"failed to generate ssh key pair at {admin_priv_key}")
                        success = False

                except Exception as exc:
                    self.logger.error(f"error generating ssh key pair: {exc}")
                    success = False
            else:
                self.logger.info(f"using existing ssh key pair at {admin_priv_key}")

        return success

    def get_global_authorized_keys(self) -> list[str]:
        """
        Resolve and return all global SSH authorized keys from the app config.

        Reads ``ssh.authorized_keys`` from :pyattr:`app_config` (the top-level
        ``boxman.yml``), resolves each entry via :pyfunc:`fetch_value`, and
        returns a list of public-key strings.

        Returns:
            List of resolved SSH public key strings.
        """
        raw_keys = (
            (self.app_config or {})
            .get("ssh", {})
            .get("authorized_keys", [])
        )
        resolved: list[str] = []
        for entry in raw_keys:
            try:
                resolved.append(self.fetch_value(entry))
            except (ValueError, FileNotFoundError) as exc:
                self.logger.warning(f"skipping unresolvable SSH key entry: {exc}")
        return resolved

    def write_global_authorized_keys_file(self, output_path: str) -> None:
        """
        Resolve global SSH keys from app_config and write them to a file.

        This bridges the Python-side boxman.yml config with the container
        entrypoint, which cannot read boxman.yml directly. The entrypoint
        reads ``global_authorized_keys`` from the bind-mounted ssh dir.

        Args:
            output_path: Path to write the authorized keys file
                         (e.g. ``<data_dir>/ssh/global_authorized_keys``).
        """
        keys = self.get_global_authorized_keys()
        if not keys:
            self.logger.info("no global authorized keys to write")
            return

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as fobj:
            for key in keys:
                fobj.write(key + "\n")
        self.logger.info(
            f"wrote {len(keys)} global authorized key(s) to {output_path}")

    @classmethod
    def fetch_value(cls, value) -> str:
        """
        Get the value referenced by *value*.

        supported reference formats:
          - Environment variable:  '${env:ENV_VAR_NAME}'
          - File contents:         'file:///abs/or/relative/path'
          -                        'file://~/path/to/file'

        for any other string the input is returned unchanged.

        Raises:
            ValueError: If the environment variable is not set.
            FileNotFoundError: If the referenced file does not exist.
        """
        if isinstance(value, str):
            # ${env:VAR}
            env_match = re.fullmatch(r"\$\{env:(.+)\}", value)
            if env_match:
                var = env_match.group(1)
                if var not in os.environ:
                    raise ValueError(f"environment variable '{var}' is not set")
                return os.environ[var]

            # file:///path/to/file
            if value.startswith("file://"):
                path = os.path.expanduser(value[len("file://"):])
                if not os.path.isfile(path):
                    raise FileNotFoundError(f"referenced file does not exist: {path}")
                with open(path, "r") as fobj:
                    return fobj.read().rstrip("\n")

        # default: return as-is
        return value

    def add_ssh_keys_to_vms(self) -> bool:
        """
        Add the generated SSH public key to all VMs to enable password-less login.

        Uses sshpass to add the public key to each VM using the admin password.

        Returns:
            bool: True if all VMs received the key successfully, False otherwise
        """
        all_successful = True

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')
            admin_pub_key = os.path.join(workdir, f"{admin_key_name}.pub")

            admin_user = cluster.get('admin_user', 'admin')
            admin_pass = self.fetch_value(cluster.get('admin_pass', None))

            if not admin_pass:
                self.logger.info(
                    f"warning: No admin password provided for cluster {cluster_name}, "
                    "cannot add SSH keys")
                all_successful = False
                continue

            if not os.path.isfile(admin_pub_key):
                self.logger.error(f"error: SSH public key {admin_pub_key} does not exist")
                all_successful = False
                continue

            self.logger.info(f"adding ssh public key to VMs in cluster {cluster_name}")

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                # get the ip addresses for this vm
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if not ip_addresses:
                    self.logger.warning(
                        f"no ip address available for vm {vm_name}, "
                        "cannot add ssh key")
                    all_successful = False
                    continue

                # use first available ip address
                ip_address = next(iter(ip_addresses.values()))

                self.logger.info(f"adding ssh key to vm {vm_name} ({ip_address})...")

                # try to add the key with exponential backoff
                success = self._try_add_ssh_key(
                    ip_address=ip_address,
                    hostname=vm_info['hostname'],
                    admin_user=admin_user,
                    admin_pass=admin_pass,
                    pub_key_path=admin_pub_key,
                    ssh_conf_path=os.path.join(workdir, cluster['ssh_config'])
                )

                if success:
                    self.logger.info(f"successfully added the ssh key to the vm {vm_name}")
                else:
                    self.logger.error(f"failed to add the ssh key to the vm {vm_name}")
                    all_successful = False

        return all_successful

    def _try_add_ssh_key(self,
                         ip_address: str,
                         hostname: str,
                         admin_user: str,
                         admin_pass: str,
                         pub_key_path: str,
                         ssh_conf_path: str) -> bool:
        """
        Try to add an SSH key to a VM with exponential backoff.

        Args:
            ip_address: IP address of the VM
            hostname: Hostname of the VM
            admin_user: Username for SSH login
            admin_pass: Password for SSH login
            pub_key_path: Path to the public key file
            ssh_conf_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        wait_time = 1  # Start with 1 second
        max_retries = 5
        max_wait = 60  # Maximum wait per attempt

        for attempt in range(1, max_retries + 1):
            self.logger.info(
                f"attempt {attempt}/{max_retries} to add ssh key (waiting {wait_time}s)")

            # use sshpass to add the public key
            cmd = (
                f'sshpass -p {admin_pass} ssh-copy-id -i {pub_key_path} '
                f'-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
                f'{admin_user}@{ip_address}'
            )

            # When using a non-local runtime, the VM is only reachable from
            # inside the container, so wrap the command with docker exec.
            cmd = self.runtime_instance.wrap_command(cmd)

            result = run(cmd, hide=False, warn=True)

            if result.ok:
                # verify we can ssh without password
                ssh_success = self._verify_ssh_connection(hostname, ssh_conf_path)

                if ssh_success:
                    return True
            else:
                self.logger.error(f"ssh key addition failed: {result.stderr.strip()}")

            # wait before next attempt with exponential backoff
            time.sleep(wait_time)
            wait_time = min(wait_time * 2, max_wait)

        return False

    def _verify_ssh_connection(self, hostname: str, ssh_config_path: str) -> bool:
        """
        Verify SSH connection to a VM.

        Args:
            hostname: Hostname of the VM
            ssh_config_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"verifying ssh connection to: {hostname}")
        ssh_cmd = f'ssh -F {ssh_config_path} {hostname} hostname'

        # When using a non-local runtime, the VM is only reachable from
        # inside the container.
        ssh_cmd = self.runtime_instance.wrap_command(ssh_cmd)

        result = run(ssh_cmd, hide=True, warn=True)

        if result.ok and result.stdout.strip():
            hostname_output = result.stdout.strip()
            self.logger.info(f"ssh connection verified: {hostname_output}")
            return True
        else:
            self.logger.error(f"ssh connection failed for {hostname}: {result.stderr.strip()}")
            return False

    def setup_ssh_access(self) -> bool:
        """
        Set up SSH access to all VMs.

        This method:
        1. Writes global authorized keys file (resolved from boxman.yml)
        2. Generates SSH keys if they don't exist
        3. Adds the public key to all vms
        4. Writes an SSH config file for easy access

        Returns:
            bool: True if all steps completed successfully, False otherwise
        """
        # write global authorized keys so they can be consumed by container
        # entrypoints or cloud-init scripts
        for _, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            global_keys_path = os.path.join(workdir, 'global_authorized_keys')
            self.write_global_authorized_keys_file(global_keys_path)

        if not self.generate_ssh_keys():
            self.logger.error("failed to generate ssh keys")
            return False

        self.write_ssh_config()

        if not self.add_ssh_keys_to_vms():
            self.logger.error("failed to add ssh keys to some vms")
            return False

        self.logger.info("\nssh access setup complete")
        self.logger.info("you can now connect to vms using the ssh config file")

        return True

    def _get_project_vm_names(self) -> list[str]:
        """
        Return the list of fully-qualified VM names that would be
        created by provisioning the current config.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        vm_names = []
        for cluster_name, cluster in self.config.get('clusters', {}).items():
            for vm_name in cluster.get('vms', {}).keys():
                vm_names.append(f"{prj_name}_{cluster_name}_{vm_name}")
        return vm_names

    def _find_existing_project_vms(self) -> list[str]:
        """
        Query libvirt and return the subset of project VM names that
        already exist (in any state).
        """
        expected = self._get_project_vm_names()
        if not expected:
            return []

        # Resolve provider config so VirshCommand uses the right
        # runtime / URI / sudo settings.
        provider_type = (
            list(self.config.get('provider', {}).keys())[0]
            if 'provider' in self.config else 'libvirt'
        )
        provider_config = self.config.get('provider', {}).get(provider_type, {})
        if self.app_config and 'providers' in self.app_config:
            app_prov = self.app_config['providers'].get(provider_type, {})
            merged = app_prov.copy()
            merged.update(provider_config)
            provider_config = merged
        if hasattr(self, 'runtime_instance'):
            provider_config = self.runtime_instance.inject_into_provider_config(
                provider_config)

        virsh = VirshCommand(provider_config=provider_config)
        result = virsh.execute("list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            self.logger.warning("could not query existing VMs via virsh")
            return []

        existing = {
            v.strip() for v in result.stdout.strip().split("\n") if v.strip()
        }
        return [vm for vm in expected if vm in existing]

    @staticmethod
    def provision(cls, cli_args):

        config = cls.config
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
        workdir = os.path.abspath(os.path.expanduser(workdir))
        if not os.path.isdir(workdir):
            os.makedirs(workdir)

        # Ensure provider config reflects runtime settings.
        # Project-level provider settings (from conf.yml) always take
        # precedence over app-level defaults (from boxman.yml).
        if hasattr(cls.provider, 'update_provider_config_with_runtime'):
            cls.provider.update_provider_config_with_runtime()

        # --- Pre-check: detect already-existing project VMs -----------
        force = getattr(cli_args, 'force', False)
        existing_vms = cls._find_existing_project_vms()

        if existing_vms:
            names = ", ".join(f"'{v}'" for v in existing_vms)
            if not force:
                cls.logger.error(
                    f"the following VM(s) already exist: {names}. "
                    f"Use --force to deprovision them first and re-provision."
                )
                return
            else:
                cls.logger.warning(
                    f"the following VM(s) already exist and will be "
                    f"deprovisioned first (--force): {names}"
                )
                cls.deprovision(cls, cli_args)
        # --------------------------------------------------------------

        try:
            cls.register_project_in_cache()
        except RuntimeError as exc:
            cls.logger.error(str(exc))
            return

        # --rebuild-templates: force-recreate all templates before provisioning
        rebuild_templates = getattr(cli_args, 'rebuild_templates', False)
        if rebuild_templates:
            cls.logger.info(
                "rebuilding all templates (--rebuild-templates implies --force "
                "for create-templates)..."
            )
            cls._create_templates_impl(requested=None, force=True)
        else:
            # Auto-create any template VMs that are referenced as base_image
            # but do not yet exist.
            cls.ensure_templates_exist()

        cls.provision_files()

        cls.define_networks()

        cls.clone_vms()

        cls.configure_cpu_mem()

        cls.configure_network_interfaces()

        cls.configure_disks()

        cls.start_vms()

        # use adaptive wait for ip address assignment
        cls.logger.info("waiting for vms to initialize and get the ip addresses...")
        wait_time = 1  # Start with 1 second
        max_wait = 600  # Maximum total wait time (10 minutes)
        total_waited = 0

        while total_waited < max_wait:
            # check if all vms have ip addresses
            if cls.get_connect_info():
                cls.logger.info(f"All vms have ip addresses (waited {total_waited}s)")
                break

            # if we get here, at least one vm doesn't have an ip yet
            cls.logger.info(f"wait {wait_time}s for ip assignment (total waited: {total_waited}s)")
            time.sleep(wait_time)
            total_waited += wait_time
            # double the wait time up to 1 minute max per iteration
            wait_time = min(wait_time * 2, 60)

        if total_waited >= max_wait:
            cls.logger.warning(
                "Reached maximum wait time. Some vms may not have ip addresses.")

        # display connection information
        cls.connect_info()

        # generate ssh keys, add them to vms, and write ssh config
        cls.setup_ssh_access()

    @staticmethod
    def deprovision(cls, cli_args):

        config = cls.config
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        cluster_name = cluster_group
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
        workdir = os.path.abspath(os.path.expanduser(workdir))

        # Ensure provider config reflects runtime settings.
        # Project-level provider settings (from conf.yml) always take
        # precedence over app-level defaults (from boxman.yml).
        if hasattr(cls.provider, 'update_provider_config_with_runtime'):
            cls.provider.update_provider_config_with_runtime()

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():

                vm_info = vm_info.copy()
                new_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                cls.provider.destroy_vm(new_vm_name)

                cls.provider.destroy_disks(
                    cluster['workdir'],
                    vm_name=new_vm_name,
                    disks=vm_info['disks']
                )

                cls.provider.destroy_vm(new_vm_name, force=True)

        cls.destroy_networks()
        # .. todo:: implement undo'ing the provisioning of the files (not important for now)
        #cls.provision_files()

        cls.unregister_from_cache()

        return

    ### start snapshot functions ####
    @staticmethod
    def snapshot_list(cls, cli_args):
        """
        List snapshots of the VMs in the cluster.
        """
        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_list(full_vm_name)

    @staticmethod
    def snapshot_take(cls, cli_args):
        """
        Take a snapshot of the VMs in the cluster.
        """
        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_take(
                    vm_name=full_vm_name,
                    vm_dir=os.path.expanduser(cluster['workdir']),
                    snapshot_name=cli_args.snapshot_name,
                    description=cli_args.snapshot_descr)

    @staticmethod
    def snapshot_restore(cls, cli_args):
        """
        Restore the state of the VMs in the cluster from a snapshot.
        """
        if not cli_args.snapshot_name:
           cls.logger.error("error: snapshot name is required")
           return

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_restore(full_vm_name, cli_args.snapshot_name)


    @staticmethod
    def snapshot_delete(cls, cli_args):
        """
        Delete a snapshot of the VMs in the cluster.
        """
        if not cli_args.snapshot_name:
            cls.logger.error("error: Snapshot name is required")
            return

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_delete(full_vm_name, cli_args.snapshot_name)
                cls.logger.info(f"Snapshot {cli_args.snapshot_name} deleted for VM {full_vm_name}")
    ### end snapshot functions ####

    ### start control vm functions ####
    def process_vm_list(self, cli_args):
        """
        Process the list of VMs to control.
        """
        retval = []
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                retval.append((full_vm_name, workdir))
        return retval

    @staticmethod
    def suspend_vm(cls, cli_args):
        """
        Suspend (pause) the VMs in the cluster.
        """
        for vm_name, _ in cls.process_vm_list(cli_args):
            cls.provider.suspend_vm(vm_name)
            cls.logger.info(f"vm {vm_name} suspended")

    @staticmethod
    def resume_vm(cls, cli_args):
        """
        Resume previously suspended VMs in the cluster.
        """
        for vm_name, _ in cls.process_vm_list(cli_args):
            cls.provider.resume_vm(vm_name)
            cls.logger.info(f"VM {vm_name} resumed")

    @staticmethod
    def save_vm(cls, cli_args):
        """
        Save the state of the VMs in the cluster to a file.
        """
        for vm_name, workdir in cls.process_vm_list(cli_args):
            cls.provider.save_vm(vm_name, workdir)

    @staticmethod
    def start_vm(cls, cli_args):
        """
        Start VMs in the cluster.
        """
        for vm_name, workdir in cls.process_vm_list(cli_args):
            if cli_args.restore:
                cls.provider.restore_vm(vm_name, workdir)
            else:
                cls.provider.start_vm(vm_name)
    ### end control vm functions ####
