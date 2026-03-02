from importlib.metadata import version as _get_version, PackageNotFoundError

package = 'boxman'
project = 'boxman'
project_no_spaces = project.replace(' ', '')

try:
    version = _get_version(package)
except PackageNotFoundError:
    version = '0.0.0.dev0'

description = 'Boxman (box manager) – declarative VM/container provisioning via YAML'
authors = ['John Smith']
authors_string = ', '.join(authors)
emails = ['john@example.com']
license = 'MIT'
copyright = '20XX ' + authors_string
url = 'https://boxman.example.com'
