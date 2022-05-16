from boxman.utils.hostnames import expand_name_range


class HostsSpecs:
    def __init__(self, hosts_spec: dict = None):
        self.specs = hosts_spec
        # .. todo:: make use of the snippet below to replicate
        #           hosts specs by hostname
        expanded_host_names = expand_name_range('node[01:05]')
        print(expanded_host_names)

