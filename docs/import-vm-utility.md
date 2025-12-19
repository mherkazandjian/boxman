# boxman-import-vm - VM Image Import Utility

## Overview

`boxman-import-vm` is a command-line utility for importing and initializing virtual machine images from URLs. It automates the process of downloading qcow2 disk images, configuring VM definitions, and registering them with libvirt.

## Features

- **Multiple Download Sources**: Support for HTTP/HTTPS URLs, Google Drive, and OneDrive
- **Automatic XML Configuration**: Edits VM XML definitions using XPath to update:
  - VM name
  - UUID (generates new UUID by default)
  - Disk image path
- **Safety Checks**: Verifies that VM name doesn't already exist (unless `--force` is used)
- **Progress Indication**: Shows download progress for large files
- **Template-based**: Uses existing VM XML or running VM as a template

## Installation

```bash
cd /path/to/boxman
pip install -r requirements.txt
python setup.py install
```

## Usage

### Basic Usage

```bash
# Import from HTTP URL using XML template file
boxman-import-vm http://example.com/ubuntu.qcow2 my-ubuntu-vm \
  --xml-template /path/to/template.xml

# Import using an existing VM as template
boxman-import-vm http://example.com/ubuntu.qcow2 my-ubuntu-vm \
  --template-vm ubuntu-base-template

# Import from Google Drive
boxman-import-vm https://drive.google.com/file/d/FILE_ID/view my-vm \
  --template-vm base-template
```

### Command-Line Options

```
Arguments:
  IMAGE_URL    URL of the qcow2 image to download [required]
  VM_NAME      Name for the new VM [required]

Options:
  --disk-dir, -d DIR           Directory to save the disk image 
                               (default: current directory)
  --xml-template, -x FILE      Path to XML template file
  --template-vm, -t NAME       Name of existing VM to use as template
  --uri, -u URI                Libvirt connection URI 
                               (default: qemu:///system)
  --keep-uuid                  Keep the original UUID instead of 
                               generating a new one
  --force, -f                  Force import even if VM with same 
                               name exists
  --help                       Show help message and exit
```

## Examples

### Example 1: Import with Custom Disk Directory

```bash
boxman-import-vm http://cloud-images.ubuntu.com/releases/24.04/ubuntu-24.04.qcow2 \
  my-ubuntu \
  --template-vm ubuntu-base \
  --disk-dir /var/lib/libvirt/images
```

This will:
1. Download the Ubuntu 24.04 image
2. Save it as `/var/lib/libvirt/images/my-ubuntu.qcow2`
3. Use the `ubuntu-base` VM as a template
4. Create a new VM named `my-ubuntu`

### Example 2: Import from Google Drive

```bash
# For large images hosted on Google Drive
boxman-import-vm \
  https://drive.google.com/file/d/1abc...xyz/view \
  windows-10-test \
  --template-vm windows-base \
  --disk-dir /data/vms
```

Google Drive URLs are automatically detected and handled with the `gdown` library, which properly handles virus scan warnings for large files.

### Example 3: Use XML Template File

```bash
# First, export XML from an existing VM:
virsh -c qemu:///system dumpxml base-template > /tmp/template.xml

# Then import with that template:
boxman-import-vm http://example.com/centos.qcow2 my-centos \
  --xml-template /tmp/template.xml \
  --disk-dir /var/lib/libvirt/images
```

### Example 4: Force Overwrite Existing VM

```bash
# This will overwrite the VM if it already exists
boxman-import-vm http://example.com/updated.qcow2 existing-vm \
  --template-vm base \
  --force
```

## Workflow

The utility follows this workflow:

1. **Validation**: Check if VM with the same name already exists (unless `--force`)
2. **Download**: Download the qcow2 image from the specified URL
3. **Template Preparation**: Get XML template from file or existing VM
4. **XML Editing**: 
   - Change VM name to the specified name
   - Generate and set new UUID (unless `--keep-uuid`)
   - Update disk source path to point to downloaded image
5. **VM Definition**: Define the new VM in libvirt using `virsh define`

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
- **Template Not Found**: Validates that XML template file or template VM exists
- **Download Failed**: Reports HTTP errors, network issues, etc.
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

1. **Use Template VMs**: Creating a base template VM with all desired settings (CPU, memory, network) makes it easy to import new VMs with consistent configuration.

2. **Disk Organization**: Use the `--disk-dir` option to organize VM disks in a centralized location.

3. **Large Files**: For large image files (>1GB), Google Drive or OneDrive may be more reliable than direct HTTP downloads.

4. **UUID Generation**: Always let the script generate a new UUID (default behavior) to avoid conflicts with other VMs.

5. **Testing**: Test the import with a small image first to verify your template and settings are correct.

## Troubleshooting

### "VM already exists" error
Use the `--force` flag to override, or choose a different VM name.

### Google Drive "quota exceeded" error
Google Drive may limit downloads for very popular files. Try again later or download manually.

### "Could not find disk source element" error
Your template XML may not have the expected disk structure. Verify the template has a `<disk type='file' device='disk'>` element.

### Permission errors
Ensure you have permission to:
- Write to the disk directory
- Execute virsh commands (may need sudo)
- Read the XML template file

## See Also

- `boxman provision` - Provision VMs from configuration files
- `boxman snapshot` - Manage VM snapshots
- `virsh` - Libvirt virtualization management tool
