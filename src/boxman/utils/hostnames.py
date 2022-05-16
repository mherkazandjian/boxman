import re


def expand_name_range(name_range: str):
    """

    :param name_range:
    :return:
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

