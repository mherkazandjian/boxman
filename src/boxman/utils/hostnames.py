import re


def expand_name_range(name_range: str):
    """
    Expand a name range into a list of strings.

    For example:
       node0[1:3] -> node01, node02, node03

    :param name_range: the name range to be exanded
    :return: a tuple of strings
    """
    host_range_only = re.search(r'\[(.*?)\]', name_range).group(1)
    name_from, name_to = host_range_only.split(':')
    n_from, n_to = int(name_from), int(name_to)
    new_name_id_format = '{:0' + str(len(name_from)) + '}'
    expanded_names = []

    for host_id in range(n_from, n_to + 1):
        pre, post = name_range.split(host_range_only)
        pre = pre.replace('[', '')
        post = post.replace(']', '')
        expanded_name = pre + new_name_id_format.format(host_id) + post
        expanded_names.append(expanded_name)

    return expanded_names



def hostname_or_key(vm_name: str, vm_info: dict):
    """
    The name a VM's ssh alias is built from: its ``hostname:``, else its key.

    An explicit ``hostname: null`` counts as absent. ``vm_info.get('hostname',
    vm_name)`` would return None for it, and two such VMs in one cluster
    would then share the alias ``<cluster>_None``.

    :param vm_name: the VM's key under ``vms:``
    :param vm_info: the VM's config block
    :return: the declared hostname as-is, or *vm_name*
    """
    declared = vm_info.get('hostname')
    return vm_name if declared is None else declared


#: One DNS label per RFC 1123: letters, digits and inner hyphens, 1-63 long.
_HOSTNAME_LABEL = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$')

#: RFC 1123's limit on a whole name, dots included.
HOSTNAME_MAX_LENGTH = 253


def hostname_problem(value) -> str | None:
    """
    Say why *value* cannot be a guest's hostname, or return None if it can.

    A dotted value is taken as a fully qualified name and checked label by
    label; nothing is appended or stripped. Booleans and numbers are refused
    outright rather than stringified: YAML reads ``hostname: 101`` as an int
    and ``hostname: no`` as False, and neither is what the author meant.

    :param value: the candidate hostname, as read from the config
    :return: a phrase that completes "the hostname ..." , or None
    """
    if isinstance(value, bool):
        return f"must be a name, got the boolean {value!r}"
    if not isinstance(value, str):
        return (f"must be a name, got the {type(value).__name__} {value!r} "
                f"(quote it in the YAML)")
    if not value:
        return "must not be empty"
    if len(value) > HOSTNAME_MAX_LENGTH:
        return (f"is {len(value)} characters long; a hostname is at most "
                f"{HOSTNAME_MAX_LENGTH}")
    for label in value.split('.'):
        if not label:
            return (f"{value!r} has an empty label (a leading, trailing or "
                    f"doubled dot)")
        if len(label) > 63:
            return (f"{value!r} has a {len(label)}-character label; each "
                    f"dot-separated label is at most 63")
        # fullmatch, not match: the pattern's $ also matches just before a
        # trailing newline, which a YAML block scalar leaves in the value
        if not _HOSTNAME_LABEL.fullmatch(label):
            return (f"{value!r} has the label {label!r}; labels use letters, "
                    f"digits and inner hyphens only")
    return None
