"""Network naming helpers for BoxmanManager."""











from typing import Any

from boxman.netlab import shared_bridges


class NamingMixin:

    @classmethod
    def full_network_name(cls,
                          project_config: dict[str, Any],
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

    def resolve_adapter_network(self,
                                adapter: dict[str, Any],
                                cluster_name: str) -> None:
        """Mutate *adapter* in place so ``network_source`` is fully qualified.

        Resolution precedence:

        1. If ``network_source`` names an entry in top-level
           ``shared_networks:``, rewrite to the host Linux bridge name
           and set ``source_type: 'bridge'``.
        2. Else if ``is_global`` is true on the adapter, leave as-is.
        3. Else apply cluster/project namespacing via
           :meth:`full_network_name`.
        """
        name = adapter['network_source']
        shared = (self.config or {}).get('shared_networks')
        if shared_bridges.is_shared_bridge(name, shared):
            adapter['network_source'] = shared_bridges.resolve_bridge(name, shared)
            adapter['source_type'] = 'bridge'
            return

        if adapter.get('is_global', False):
            return

        adapter['network_source'] = self.full_network_name(
            project_config=self.config,
            cluster_name=cluster_name,
            network_name=name,
        )
