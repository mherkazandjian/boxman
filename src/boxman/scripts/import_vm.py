#!/usr/bin/env python
"""
Utility script for importing/initializing VM images from URLs.

This script downloads a .tar.gz file containing a VM package (manifest, XML, and disk image),
extracts it, and uses the manifest to set up the VM in libvirt.
"""

import os
import uuid
import json
import tempfile
import tarfile
import shutil
import traceback
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import typer
import requests
from lxml import etree
from invoke import run

app = typer.Typer(help="Import and initialize VM images from URLs")



def download_package_http(package_url: str, dest_path: str) -> bool:
    """
    Download a package from HTTP/HTTPS URL.
    
    Args:
        package_url: HTTP/HTTPS URL to download from
        dest_path: Destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        typer.echo(f"Downloading from {package_url}...")
        response = requests.get(package_url, stream=True, allow_redirects=True, timeout=30)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        
        with open(dest_path, 'wb') as f:
            if total_size == 0:
                # No content-length header, use chunked reading
                chunk_size = 8192
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
            else:
                downloaded = 0
                chunk_size = 8192
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = (downloaded / total_size) * 100
                        typer.echo(f"\rProgress: {percent:.1f}%", nl=False)
                typer.echo()  # New line after progress
        
        typer.echo(f"✓ Package downloaded")
        return True
        
    except requests.exceptions.RequestException as e:
        typer.echo(f"✗ Failed to download package: {e}", err=True)
        return False
    except Exception as e:
        typer.echo(f"✗ Error downloading package: {e}", err=True)
        return False


def download_package_google_drive(package_url: str, dest_path: str) -> bool:
    """
    Download a package from Google Drive.
    
    Args:
        package_url: Google Drive URL
        dest_path: Destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        import gdown
        typer.echo(f"Downloading from Google Drive...")
        result = gdown.download(package_url, dest_path, quiet=False, fuzzy=True)
        
        # gdown.download may return None on failure without raising an exception
        if not result or not os.path.isfile(dest_path) or os.path.getsize(dest_path) == 0:
            typer.echo(f"✗ Error downloading from Google Drive: download did not complete successfully", err=True)
            return False
        
        typer.echo(f"✓ Package downloaded from Google Drive")
        return True
        
    except ImportError:
        typer.echo("✗ gdown package not installed. Please install it with: pip install gdown", err=True)
        return False
    except Exception as e:
        typer.echo(f"✗ Error downloading from Google Drive: {e}", err=True)
        return False


def copy_local_package(package_path: str, dest_path: str) -> bool:
    """
    Copy a package from local filesystem.
    
    Args:
        package_path: Local filesystem path
        dest_path: Destination path to copy to
        
    Returns:
        True if successful, False otherwise
    """
    try:
        typer.echo(f"Copying from local file: {package_path}...")
        
        if not os.path.exists(package_path):
            typer.echo(f"✗ Local file not found: {package_path}", err=True)
            return False
        
        if not os.path.isfile(package_path):
            typer.echo(f"✗ Path is not a file: {package_path}", err=True)
            return False
        
        shutil.copy2(package_path, dest_path)
        typer.echo(f"✓ Package copied from local filesystem")
        return True
        
    except Exception as e:
        typer.echo(f"✗ Error copying local file: {e}", err=True)
        return False


def download_and_extract_package(package_url: str, extract_dir: str) -> bool:
    """
    Download and extract a .tar.gz VM package file.
    
    Supports:
    - HTTP/HTTPS URLs
    - Google Drive URLs (drive.google.com)
    - Local file paths (file:// URLs)
    
    Args:
        package_url: URL or file path to the .tar.gz package
        extract_dir: Directory to extract the package to
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Create temporary file for the package
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        
        # Determine source type and download/copy accordingly
        parsed_url = urlparse(package_url)
        
        if parsed_url.scheme == 'file':
            # Local file path
            local_path = parsed_url.path
            # On Windows, handle file:///C:/path format
            if os.name == 'nt' and local_path.startswith('/') and len(local_path) > 2 and local_path[2] == ':':
                local_path = local_path[1:]
            
            if not copy_local_package(local_path, tmp_path):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                return False
                
        elif parsed_url.netloc.lower() in ['drive.google.com', 'docs.google.com']:
            # Google Drive URL
            if not download_package_google_drive(package_url, tmp_path):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                return False
                
        else:
            # HTTP/HTTPS URL
            if not download_package_http(package_url, tmp_path):
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                return False
        
        # Extract the tar.gz file
        typer.echo(f"Extracting package...")
        try:
            with tarfile.open(tmp_path, 'r:gz') as tar:
                # Security check: ensure no path traversal
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        typer.echo(f"✗ Archive contains unsafe path: {member.name}", err=True)
                        os.unlink(tmp_path)
                        return False
                
                tar.extractall(path=extract_dir)
            
            typer.echo(f"✓ Package extracted to {extract_dir}")
            
        finally:
            # Clean up the downloaded tar.gz file
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        
        return True
        
    except tarfile.TarError as e:
        typer.echo(f"✗ Failed to extract package: {e}", err=True)
        return False
    except Exception as e:
        typer.echo(f"✗ Error processing package: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return False


def load_manifest_from_dir(extract_dir: str) -> Optional[Dict[str, Any]]:
    """
    Load and parse a JSON manifest file from the extracted directory.
    
    The manifest should contain:
    - xml_path: Relative path to the VM XML definition file
    - image_path: Relative path to the qcow2 disk image file
    
    Args:
        extract_dir: Directory where the package was extracted
        
    Returns:
        Dictionary containing manifest data, or None if failed
    """
    try:
        # Look for manifest.json in the extract directory
        manifest_path = os.path.join(extract_dir, 'manifest.json')
        
        if not os.path.exists(manifest_path):
            typer.echo(f"✗ Manifest file not found: {manifest_path}", err=True)
            return None
        
        typer.echo(f"Reading manifest from {manifest_path}...")
        
        with open(manifest_path, 'r') as f:
            manifest = json.load(f)
        
        # Validate required fields
        if 'xml_path' not in manifest:
            typer.echo("✗ Manifest missing required field: 'xml_path'", err=True)
            return None
        
        if 'image_path' not in manifest:
            typer.echo("✗ Manifest missing required field: 'image_path'", err=True)
            return None
        
        typer.echo(f"✓ Manifest loaded successfully")
        typer.echo(f"  XML path: {manifest['xml_path']}")
        typer.echo(f"  Image path: {manifest['image_path']}")
        
        return manifest
        
    except json.JSONDecodeError as e:
        typer.echo(f"✗ Failed to parse manifest JSON: {e}", err=True)
        return None
    except Exception as e:
        typer.echo(f"✗ Error loading manifest: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return None


def edit_vm_xml(xml_path: str, new_vm_name: str, disk_path: str, change_uuid: bool = True) -> bool:
    """
    Edit the VM XML definition to change the name, UUID, and disk path.
    
    Args:
        xml_path: Path to the XML file to edit
        new_vm_name: The new name for the VM
        disk_path: The path to the disk image
        change_uuid: Whether to generate a new UUID (default: True)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        typer.echo(f"Editing VM XML: {xml_path}")
        
        # Parse the XML file
        tree = etree.parse(xml_path)
        root = tree.getroot()
        
        # Change the VM name using XPath
        name_elements = root.xpath('/domain/name')
        if name_elements:
            name_elements[0].text = new_vm_name
            typer.echo(f"  ✓ Changed VM name to: {new_vm_name}")
        else:
            typer.echo("  ✗ Could not find name element in XML", err=True)
            return False
        
        # Change the UUID using XPath
        if change_uuid:
            uuid_elements = root.xpath('/domain/uuid')
            if uuid_elements:
                new_uuid = str(uuid.uuid4())
                uuid_elements[0].text = new_uuid
                typer.echo(f"  ✓ Changed UUID to: {new_uuid}")
            else:
                typer.echo("  ⚠ Warning: Could not find UUID element in XML")
        
        # Change the disk source path using XPath
        # Look for the boot disk (typically the first disk with type='file' and device='disk')
        disk_source_elements = root.xpath("/domain/devices/disk[@type='file'][@device='disk']/source[@file]")
        if disk_source_elements:
            disk_source_elements[0].set('file', disk_path)
            typer.echo(f"  ✓ Changed disk source to: {disk_path}")
        else:
            typer.echo("  ✗ Could not find disk source element in XML", err=True)
            return False
        
        # Write the modified XML back to the file
        tree.write(xml_path, encoding='utf-8', xml_declaration=True, pretty_print=True)
        typer.echo(f"✓ XML file updated successfully")
        
        return True
        
    except Exception as e:
        typer.echo(f"✗ Error editing XML file: {e}", err=True)
        typer.echo(traceback.format_exc(), err=True)
        return False


def check_vm_exists(vm_name: str, uri: str = "qemu:///system") -> bool:
    """
    Check if a VM with the given name already exists.
    
    Args:
        vm_name: The name of the VM to check
        uri: The libvirt URI (default: qemu:///system)
        
    Returns:
        True if the VM exists, False otherwise
    """
    try:
        result = run(
            f"virsh -c {uri} list --all --name",
            hide=True,
            warn=True
        )
        
        if result.ok:
            vm_list = [vm for vm in result.stdout.strip().split('\n') if vm]
            return vm_name in vm_list
        
        return False
        
    except Exception as e:
        typer.echo(f"⚠ Warning: Could not check if VM exists: {e}", err=True)
        return False


def define_vm(xml_path: str, uri: str = "qemu:///system") -> bool:
    """
    Define a VM from an XML file using virsh.
    
    Args:
        xml_path: Path to the XML file
        uri: The libvirt URI (default: qemu:///system)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        typer.echo(f"Defining VM from {xml_path}...")
        
        result = run(
            f"virsh -c {uri} define {xml_path}",
            hide=False,
            warn=True
        )
        
        if result.ok:
            typer.echo(f"✓ VM defined successfully")
            return True
        else:
            typer.echo(f"✗ Failed to define VM: {result.stderr}", err=True)
            return False
            
    except Exception as e:
        typer.echo(f"✗ Error defining VM: {e}", err=True)
        return False


@app.command()
def import_image(
    package_url: str = typer.Option(..., "--url", help="URL or file path of the .tar.gz VM package (http://, https://, file://, or Google Drive)"),
    vm_name: str = typer.Option(..., "--name", help="Name for the new VM"),
    disk_dir: str = typer.Option(
        None,
        "--disk-dir",
        "-d",
        help="Directory to save the disk image (default: current directory)"
    ),
    uri: str = typer.Option(
        "qemu:///system",
        "--uri",
        "-u",
        help="Libvirt connection URI"
    ),
    keep_uuid: bool = typer.Option(
        False,
        "--keep-uuid",
        help="Keep the original UUID (default: generate new UUID)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force import even if VM with same name exists"
    ),
):
    """
    Import and initialize a VM from a .tar.gz package.
    
    Supports multiple source types:
    - HTTP/HTTPS URLs: http://example.com/vm-package.tar.gz
    - Google Drive URLs: https://drive.google.com/file/d/FILE_ID/view
    - Local files: file:///path/to/vm-package.tar.gz
    
    The package should contain:
    - A manifest.json file with relative paths to the XML and disk image
    - The VM XML definition file
    - The disk image file (qcow2)
    
    Manifest format:
    {
        "xml_path": "vm-definition.xml",
        "image_path": "disk-image.qcow2"
    }
    
    This command downloads/copies the package, extracts it, reads the manifest,
    edits the XML to use the new VM name and disk path, and defines the VM in libvirt.
    
    Examples:
    
        # Import from HTTP URL
        boxman-import-vm --url http://example.com/vm-package.tar.gz --name my-ubuntu-vm
        
        # Import from Google Drive
        boxman-import-vm --url https://drive.google.com/file/d/FILE_ID/view --name my-vm
        
        # Import from local file
        boxman-import-vm --url file:///path/to/vm-package.tar.gz --name my-vm
        
        # Import with custom disk directory
        boxman-import-vm --url http://example.com/vm-package.tar.gz --name my-vm --disk-dir /var/lib/libvirt/images
    """
    
    typer.echo("=" * 70)
    typer.echo("VM Image Import Utility")
    typer.echo("=" * 70)
    
    # Check if VM already exists
    if check_vm_exists(vm_name, uri):
        if not force:
            typer.echo(f"✗ VM '{vm_name}' already exists. Use --force to override.", err=True)
            raise typer.Exit(code=1)
        else:
            typer.echo(f"⚠ Warning: VM '{vm_name}' already exists but --force was specified")
    
    # Create a temporary directory for extraction
    extract_dir = tempfile.mkdtemp(prefix='boxman-import-')
    temp_xml_file = None
    
    try:
        # Download and extract the package
        typer.echo(f"\n[1/5] Downloading and extracting package...")
        if not download_and_extract_package(package_url, extract_dir):
            typer.echo("✗ Failed to download or extract package", err=True)
            raise typer.Exit(code=1)
        
        # Load the manifest
        typer.echo(f"\n[2/5] Loading manifest...")
        manifest = load_manifest_from_dir(extract_dir)
        if manifest is None:
            typer.echo("✗ Failed to load manifest", err=True)
            raise typer.Exit(code=1)
        
        xml_rel_path = manifest['xml_path']
        image_rel_path = manifest['image_path']
        
        # Construct full paths from the extracted directory
        xml_source_path = os.path.join(extract_dir, xml_rel_path)
        image_source_path = os.path.join(extract_dir, image_rel_path)
        
        # Validate that the files exist
        if not os.path.exists(xml_source_path):
            typer.echo(f"✗ XML file not found: {xml_source_path}", err=True)
            raise typer.Exit(code=1)
        
        if not os.path.exists(image_source_path):
            typer.echo(f"✗ Disk image file not found: {image_source_path}", err=True)
            raise typer.Exit(code=1)
        
        # Determine disk directory
        if disk_dir is None:
            disk_dir = os.getcwd()
        disk_dir = os.path.abspath(os.path.expanduser(disk_dir))
        
        # Create disk directory if it doesn't exist
        os.makedirs(disk_dir, exist_ok=True)
        
        # Determine disk filename from the source image
        image_extension = os.path.splitext(image_source_path)[1]
        if not image_extension:
            image_extension = '.qcow2'
        disk_filename = f"{vm_name}{image_extension}"
        disk_path = os.path.join(disk_dir, disk_filename)
        
        # Copy the disk image to the destination
        typer.echo(f"\n[3/5] Copying disk image to {disk_path}...")
        try:
            shutil.copy2(image_source_path, disk_path)
            typer.echo(f"✓ Disk image copied")
        except Exception as e:
            typer.echo(f"✗ Failed to copy disk image: {e}", err=True)
            raise typer.Exit(code=1)
        
        # Create a temporary copy of the XML for editing
        typer.echo(f"\n[4/5] Preparing VM configuration...")
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            temp_xml_file = f.name
        shutil.copy2(xml_source_path, temp_xml_file)
        
        # Edit the XML
        if not edit_vm_xml(temp_xml_file, vm_name, disk_path, change_uuid=not keep_uuid):
            typer.echo("✗ Failed to edit XML", err=True)
            raise typer.Exit(code=1)
        
        # Define the VM
        typer.echo(f"\n[5/5] Defining VM in libvirt...")
        if not define_vm(temp_xml_file, uri):
            typer.echo("✗ Failed to define VM", err=True)
            raise typer.Exit(code=1)
        
        typer.echo("\n" + "=" * 70)
        typer.echo(f"✓ Successfully imported VM '{vm_name}'")
        typer.echo(f"  Disk image: {disk_path}")
        typer.echo(f"  Connection URI: {uri}")
        typer.echo("=" * 70)
        
    finally:
        # Clean up temporary files and directories
        if temp_xml_file and os.path.exists(temp_xml_file):
            os.unlink(temp_xml_file)
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)


def main():
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
