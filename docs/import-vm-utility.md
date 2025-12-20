# boxman-import-vm - VM Image Import Utility

## Overview

`boxman-import-vm` is a command-line utility for importing and initializing virtual machine images from a `.tar.gz` package. The package contains a manifest file with relative paths to the VM XML definition and disk image, along with the actual files. This tool automates the process of extracting the package, configuring the VM definition with the new name and disk path, and registering it with libvirt.

The utility supports multiple source types:
- **HTTP/HTTPS URLs**: Direct downloads from web servers
- **Google Drive URLs**: Downloads from Google Drive with automatic handling of large files
- **Local files**: Using `file://` URLs to import from the local filesystem

## Features

- **Package-based Import**: Downloads and extracts a `.tar.gz` file containing the VM configuration and disk image
- **Multiple Source Types**: HTTP/HTTPS, Google Drive, and local filesystem support
- **Manifest-driven Configuration**: Uses a JSON manifest with relative paths to locate files within the package
- **Automatic XML Configuration**: Edits VM XML definitions using XPath to update:
  - VM name
  - UUID (generates new UUID by default)
  - Disk image path
- **Safety Checks**: Verifies that VM name doesn't already exist (unless `--force` is used)
- **Progress Indication**: Shows download progress for large packages

## Package Format

The `.tar.gz` package should contain:

1. **manifest.json**: A JSON file describing the package contents
2. **VM XML file**: The libvirt domain XML definition
3. **Disk image file**: The qcow2 (or other format) disk image

### Manifest Format

The `manifest.json` file should have the following structure:

```json
{
  "xml_path": "vm-definition.xml",
  "image_path": "disk-image.qcow2"
}
```

- **xml_path**: Relative path to the VM XML definition file within the extracted package
- **image_path**: Relative path to the disk image file within the extracted package

### Example Package Structure

```
vm-package.tar.gz
├── manifest.json
├── ubuntu-vm.xml
└── ubuntu-disk.qcow2
```

With manifest.json containing:
```json
{
  "xml_path": "ubuntu-vm.xml",
  "image_path": "ubuntu-disk.qcow2"
}
```

## Installation

```bash
cd /path/to/boxman
pip install -r requirements.txt
python setup.py install
```

## Usage

### Basic Usage

```bash
# Import from HTTP URL
boxman-import-vm --url http://example.com/vm-package.tar.gz --name my-ubuntu-vm

# Import from Google Drive
boxman-import-vm --url https://drive.google.com/file/d/FILE_ID/view --name my-vm

# Import from local file
boxman-import-vm --url file:///path/to/vm-package.tar.gz --name my-vm

# Import with custom disk directory
boxman-import-vm --url http://example.com/vm-package.tar.gz --name my-vm \
  --disk-dir /var/lib/libvirt/images

# Import with force flag to overwrite existing VM
boxman-import-vm --url file:///tmp/vm-package.tar.gz --name existing-vm --force
```

### Command-Line Options

```
Options:
  --url URL                    URL or file path of the .tar.gz VM package
                               Supports: http://, https://, file://, or Google Drive URLs
                               [required]
  --name NAME                  Name for the new VM [required]
  --disk-dir, -d DIR           Directory to save the disk image 
                               (default: current directory)
  --uri, -u URI                Libvirt connection URI 
                               (default: qemu:///system)
  --keep-uuid                  Keep the original UUID instead of 
                               generating a new one
  --force, -f                  Force import even if VM with same 
                               name exists
  --help                       Show help message and exit
```

## Examples

### Example 1: Import from HTTP URL

```bash
boxman-import-vm --url http://example.com/ubuntu-vm.tar.gz --name my-ubuntu
```

This will:
1. Download the VM package from the HTTP URL
2. Extract it to a temporary directory
3. Read the manifest.json file
4. Copy the disk image to the current directory as `my-ubuntu.qcow2` (or appropriate extension)
5. Edit the XML to use the new VM name and disk path
6. Define the VM in libvirt

### Example 2: Import from Google Drive

```bash
boxman-import-vm --url https://drive.google.com/file/d/1ABC...XYZ/view --name my-windows-vm
```

This will download the package from Google Drive. The `gdown` library handles Google Drive's virus scan warnings for large files automatically.

### Example 3: Import from Local File

```bash
# Using absolute path
boxman-import-vm --url file:///var/backups/vm-package.tar.gz --name restored-vm

# Using relative path (file:// + full path)
boxman-import-vm --url file:///home/user/vms/backup.tar.gz --name my-backup-vm
```

This will copy the package from the local filesystem without needing to download it.

### Example 4: Import with Custom Disk Directory

```bash
boxman-import-vm --url http://example.com/centos-vm.tar.gz --name my-centos \
  --disk-dir /var/lib/libvirt/images
```

This will save the disk image to `/var/lib/libvirt/images/my-centos.qcow2`.

### Example 5: Force Overwrite Existing VM

```bash
# This will overwrite the VM if it already exists
boxman-import-vm --url file:///tmp/updated-vm.tar.gz --name existing-vm --force
```

## Workflow

The utility follows this workflow:

1. **Validation**: Check if VM with the same name already exists (unless `--force`)
2. **Download/Copy Package**: 
   - For HTTP/HTTPS: Download the .tar.gz package
   - For Google Drive: Download using `gdown` with large file support
   - For file:// URLs: Copy from local filesystem
3. **Extract Package**: Extract the package to a temporary directory with security checks
4. **Load Manifest**: Read and validate the manifest.json file
5. **Copy Files**: Copy the disk image from the extracted directory to the target location
6. **XML Editing**: 
   - Change VM name to the specified name
   - Generate and set new UUID (unless `--keep-uuid`)
   - Update disk source path to point to copied image
7. **VM Definition**: Define the new VM in libvirt using `virsh define`
8. **Cleanup**: Remove temporary extraction directory

## Supported URL Types

The utility supports three types of package sources:

### HTTP/HTTPS URLs
Direct downloads from any web server:
```bash
http://example.com/vm-package.tar.gz
https://example.com/downloads/ubuntu-vm.tar.gz
```

### Google Drive URLs
Downloads from Google Drive with automatic large file handling:
```bash
https://drive.google.com/file/d/FILE_ID/view
https://docs.google.com/uc?id=FILE_ID
```

The utility uses the `gdown` library which handles:
- Large file downloads
- Google Drive virus scan warnings
- Authentication for public files

**Note**: The `gdown` package must be installed: `pip install gdown`

### Local File Paths
Import from the local filesystem using `file://` URLs:
```bash
file:///absolute/path/to/vm-package.tar.gz
file:///home/user/backups/vm-package.tar.gz
```

This is useful for:
- Importing from local backups
- Testing package creation before uploading
- Scenarios where the package is already on the local system

## XML Editing Details

The script uses XPath to precisely edit the VM XML definition:

- **VM Name**: Updates `/domain/name` element
- **UUID**: Updates `/domain/uuid` element with a newly generated UUID
- **Disk Path**: Updates `/domain/devices/disk[@type='file'][@device='disk']/source[@file]` attribute

This ensures that only the necessary parts of the XML are modified while preserving all other VM configuration settings.

## Supported URL Types

### HTTP/HTTPS URLs
Direct downloads from any HTTP or HTTPS URL:
```bash
http://example.com/image.qcow2
https://example.com/downloads/vm-image.qcow2
```

### Google Drive URLs
Supports Google Drive share links:
```bash
https://drive.google.com/file/d/FILE_ID/view
https://docs.google.com/uc?id=FILE_ID
```

The utility uses the `gdown` library which handles:
- Large file downloads
- Google Drive virus scan warnings
- Authentication for public files

### OneDrive URLs
Supports OneDrive share links:
```bash
https://onedrive.live.com/...
https://1drv.ms/...
```

The utility attempts to convert share links to direct download links.

## Error Handling

The script provides clear error messages for common issues:

- **VM Already Exists**: Stops unless `--force` is specified
- **Package Download Failed**: Reports HTTP errors, network issues, etc.
- **Package Extract Failed**: Reports tar/gzip errors, validates for path traversal attacks
- **Manifest Not Found**: Validates manifest.json exists in extracted package
- **Manifest Parse Error**: Reports JSON parsing errors
- **Missing Manifest Fields**: Validates xml_path and image_path are present
- **Files Not Found**: Validates that XML and disk image files exist at specified paths
- **XML Parsing Errors**: Reports issues with malformed XML
- **Disk Path Not Found**: Verifies the disk element exists in XML

## Requirements

- Python 3.7+
- libvirt and virsh
- Required Python packages:
  - `typer` - CLI framework
  - `requests` - HTTP downloads
  - `gdown` - Google Drive downloads
  - `lxml` - XML parsing and editing
  - `invoke` - Command execution

## Tips

1. **Create Packages**: Package your VM images and definitions together for easy distribution and repeatability.

2. **Disk Organization**: Use the `--disk-dir` option to organize VM disks in a centralized location.

3. **Package Hosting**: Host your `.tar.gz` packages on a web server or CDN for fast downloads.

4. **UUID Generation**: Always let the script generate a new UUID (default behavior) to avoid conflicts with other VMs.

5. **Testing**: Test the import with a small package first to verify your package structure is correct.

## Creating a VM Package

To create a package for distribution:

1. Export the VM XML definition (if using an existing VM):
   ```bash
   virsh -c qemu:///system dumpxml my-vm > vm-definition.xml
   ```

2. Create a manifest.json file:
   ```json
   {
     "xml_path": "vm-definition.xml",
     "image_path": "disk-image.qcow2"
   }
   ```

3. Package the files:
   ```bash
   tar -czf my-vm-package.tar.gz manifest.json vm-definition.xml disk-image.qcow2
   ```

4. Upload the package to your web server or file hosting service

5. Share the URL with users who can then import with:
   ```bash
   boxman-import-vm --url http://yourserver.com/my-vm-package.tar.gz --name imported-vm
   ```

## Troubleshooting

### "VM already exists" error
Use the `--force` flag to override, or choose a different VM name.

### "Manifest missing required field" error
Ensure your manifest.json contains both `xml_path` and `image_path` fields with relative paths.

### "Package contains unsafe path" error
The tar.gz file contains absolute paths or path traversal sequences (..). Recreate the package with relative paths only.

### "XML file not found" or "Disk image file not found" error
The paths specified in manifest.json don't exist in the extracted package. Verify the manifest paths match the actual files in the archive.

### "Could not find disk source element" error
Your VM XML may not have the expected disk structure. Verify the XML has a `<disk type='file' device='disk'>` element.

### Permission errors
Ensure you have permission to:
- Write to the disk directory
- Execute virsh commands (may need sudo)
- Extract files to temporary directories

## See Also

- `boxman provision` - Provision VMs from configuration files
- `boxman snapshot` - Manage VM snapshots
- `virsh` - Libvirt virtualization management tool
