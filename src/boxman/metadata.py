from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version

try:
    version = _get_version('boxman')
except PackageNotFoundError:
    version = '0.0.0.dev0'
