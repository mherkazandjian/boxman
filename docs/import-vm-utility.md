# boxman-import-vm - VM Image Import Utility

## Overview

`boxman-import-vm` is a command-line utility for importing and initializing virtual machine images from a JSON manifest. The manifest contains URLs for both the VM XML definition and the disk image. This tool automates the process of downloading both files, configuring the VM definition with the new name and disk path, and registering it with libvirt.

## Features

- **JSON Manifest-based Import**: Downloads VM configuration and disk image from URLs specified in a JSON manifest
- **Automatic XML Configuration**: Edits VM XML definitions using XPath to update:
  - VM name
  - UUID (generates new UUID by default)
  - Disk image path
- **Multiple Download Sources**: Support for HTTP/HTTPS URLs, Google Drive, and OneDrive
- **Safety Checks**: Verifies that VM name doesn't already exist (unless `--force` is used)
- **Progress Indication**: Shows download progress for large files

## Manifest Format

The JSON manifest file should contain the following structure:

```json
{
  "xml_url": "http://example.com/vm-definition.xml",
  "image_url": "http://example.com/disk-image.qcow2"
}
```

- **xml_url**: URL to the libvirt domain XML definition file
- **image_url**: URL to the qcow2 disk image file

## Installation

```bash
cd /path/to/boxman
pip install -r requirements.txt
python setup.py install
```

## Usage

### Basic Usage

```bash
# Import from a JSON manifest
boxman-import-vm --url http://example.com/manifest.json --name my-ubuntu-vm

# Import with custom disk directory
boxman-import-vm --url http://example.com/manifest.json --name my-vm \
  --disk-dir /var/lib/libvirt/images

# Import with force flag to overwrite existing VM
boxman-import-vm --url http://example.com/manifest.json --name existing-vm --force
```

### Command-Line Options

```
Options:
  --url URL                    URL of the JSON manifest file [required]
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

### Example 1: Basic Import

```bash
boxman-import-vm --url http://example.com/ubuntu-manifest.json --name my-ubuntu
```

This will:
1. Download the manifest from the URL
2. Download the VM XML definition from the xml_url in the manifest
3. Download the disk image from the image_url in the manifest
4. Save the disk as `my-ubuntu.qcow2` in the current directory
5. Edit the XML to use the new VM name and disk path
6. Define the VM in libvirt

### Example 2: Import with Custom Disk Directory

```bash
boxman-import-vm --url http://example.com/centos-manifest.json --name my-centos \
  --disk-dir /var/lib/libvirt/images
```

This will save the disk image to `/var/lib/libvirt/images/my-centos.qcow2`.

### Example 3: Import from Google Drive Manifest

```bash
# Manifest hosted on Google Drive
boxman-import-vm --url https://drive.google.com/file/d/MANIFEST_ID/view --name my-vm
```

The manifest URLs (xml_url and image_url) can also point to Google Drive or OneDrive files. The utility will handle the download appropriately.

### Example 4: Force Overwrite Existing VM

```bash
# This will overwrite the VM if it already exists
boxman-import-vm --url http://example.com/updated-manifest.json --name existing-vm --force
```

## Workflow

The utility follows this workflow:

1. **Validation**: Check if VM with the same name already exists (unless `--force`)
2. **Download Manifest**: Download and parse the JSON manifest file
3. **Download XML**: Download the VM XML definition from the xml_url in the manifest
4. **Download Image**: Download the qcow2 disk image from the image_url in the manifest
5. **XML Editing**: 
   - Change VM name to the specified name
   - Generate and set new UUID (unless `--keep-uuid`)
   - Update disk source path to point to downloaded image
6. **VM Definition**: Define the new VM in libvirt using `virsh define`

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
- **Manifest Download Failed**: Reports HTTP errors, network issues, etc.
- **Manifest Parse Error**: Reports JSON parsing errors
- **Missing Manifest Fields**: Validates xml_url and image_url are present
- **XML Download Failed**: Reports download errors for XML file
- **Image Download Failed**: Reports download errors for disk image
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

1. **Create Manifests**: You can create manifest files for your VM images to make importing easy and repeatable.

2. **Disk Organization**: Use the `--disk-dir` option to organize VM disks in a centralized location.

3. **Large Files**: For large image files (>1GB), consider hosting the manifest, XML, and image on Google Drive or a CDN.

4. **UUID Generation**: Always let the script generate a new UUID (default behavior) to avoid conflicts with other VMs.

5. **Testing**: Test the import with a small image first to verify your manifest and settings are correct.

## Creating a Manifest

To create a manifest for your VM, you'll need to:

1. Export the VM XML definition (if using an existing VM):
   ```bash
   virsh -c qemu:///system dumpxml my-template-vm > vm-definition.xml
   ```

2. Upload both the XML file and the qcow2 image to a web server or cloud storage

3. Create a JSON manifest file:
   ```json
   {
     "xml_url": "http://yourserver.com/vm-definition.xml",
     "image_url": "http://yourserver.com/disk-image.qcow2"
   }
   ```

4. Upload the manifest file and share its URL with users

## Troubleshooting

### "VM already exists" error
Use the `--force` flag to override, or choose a different VM name.

### "Manifest missing required field" error
Ensure your JSON manifest contains both `xml_url` and `image_url` fields.

### Google Drive "quota exceeded" error
Google Drive may limit downloads for very popular files. Try again later or use a different hosting service.

### "Could not find disk source element" error
Your VM XML may not have the expected disk structure. Verify the XML has a `<disk type='file' device='disk'>` element.

### Permission errors
Ensure you have permission to:
- Write to the disk directory
- Execute virsh commands (may need sudo)
- Access the manifest and resource URLs

## See Also

- `boxman provision` - Provision VMs from configuration files
- `boxman snapshot` - Manage VM snapshots
- `virsh` - Libvirt virtualization management tool
