#!/usr/bin/env python
"""
Utility script for importing/initializing VM images from URLs.

This script downloads a JSON manifest containing URLs for VM XML definition and qcow2 image,
then downloads both files, edits the VM XML to use the new image and name, and defines the VM in libvirt.
"""

import os
import sys
import uuid
import json
import tempfile
import traceback
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import typer
import requests
from lxml import etree
from invoke import run

app = typer.Typer(help="Import and initialize VM images from URLs")


def download_file_http(url: str, dest_path: str) -> bool:
    """
    Download a file from an HTTP/HTTPS URL with progress indication.
    
    Args:
        url: The URL to download from
        dest_path: The destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        typer.echo(f"Downloading from {url}...")
        
        response = requests.get(url, stream=True, allow_redirects=True)
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
        
        typer.echo(f"✓ Downloaded to {dest_path}")
        return True
        
    except Exception as e:
        typer.echo(f"✗ Error downloading file: {e}", err=True)
        return False


def download_file_google_drive(url: str, dest_path: str) -> bool:
    """
    Download a file from Google Drive with support for large files.
    
    Args:
        url: The Google Drive URL
        dest_path: The destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        import gdown
        typer.echo(f"Downloading from Google Drive...")
        gdown.download(url, dest_path, quiet=False, fuzzy=True)
        typer.echo(f"✓ Downloaded to {dest_path}")
        return True
    except ImportError:
        typer.echo("✗ gdown package not installed. Please install it with: pip install gdown", err=True)
        return False
    except Exception as e:
        typer.echo(f"✗ Error downloading from Google Drive: {e}", err=True)
        return False


def download_file_onedrive(url: str, dest_path: str) -> bool:
    """
    Download a file from OneDrive.
    
    Note: OneDrive direct download support is limited. For best results,
    use direct download links or consider manual download for OneDrive files.
    
    Args:
        url: The OneDrive URL
        dest_path: The destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Parse the URL to safely check the domain
        parsed = urlparse(url)
        download_url = url
        
        # Attempt to construct direct download URL for onedrive.live.com links
        if parsed.netloc == 'onedrive.live.com' and 'download' not in parsed.query:
            # Try appending download parameter
            separator = '&' if parsed.query else '?'
            download_url = f"{url}{separator}download=1"
        
        typer.echo("⚠ Note: OneDrive support is limited. Direct HTTP URLs are recommended.", err=True)
        return download_file_http(download_url, dest_path)
        
    except Exception as e:
        typer.echo(f"✗ Error downloading from OneDrive: {e}", err=True)
        typer.echo("  Consider using a direct HTTP download link instead.", err=True)
        return False


def download_image(url: str, dest_path: str) -> bool:
    """
    Download an image from a URL. Automatically detects the source type.
    
    Args:
        url: The URL to download from (HTTP, Google Drive, or OneDrive)
        dest_path: The destination path to save the file
        
    Returns:
        True if successful, False otherwise
    """
    # Parse the URL to safely detect the source type
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        
        # Check for Google Drive domains
        if netloc in ['drive.google.com', 'docs.google.com']:
            return download_file_google_drive(url, dest_path)
        # Check for OneDrive/SharePoint domains
        elif netloc in ['onedrive.live.com', '1drv.ms'] or netloc.endswith('.sharepoint.com'):
            return download_file_onedrive(url, dest_path)
        else:
            return download_file_http(url, dest_path)
    except Exception as e:
        typer.echo(f"✗ Error parsing URL: {e}", err=True)
        # Fall back to HTTP download
        return download_file_http(url, dest_path)


def download_manifest(manifest_url: str) -> Optional[Dict[str, Any]]:
    """
    Download and parse a JSON manifest file.
    
    The manifest should contain:
    - xml_url: URL to the VM XML definition
    - image_url: URL to the qcow2 disk image
    
    Args:
        manifest_url: URL to the JSON manifest
        
    Returns:
        Dictionary containing manifest data, or None if failed
    """
    try:
        typer.echo(f"Downloading manifest from {manifest_url}...")
        
        response = requests.get(manifest_url, allow_redirects=True)
        response.raise_for_status()
        
        manifest = response.json()
        
        # Validate required fields
        if 'xml_url' not in manifest:
            typer.echo("✗ Manifest missing required field: 'xml_url'", err=True)
            return None
        
        if 'image_url' not in manifest:
            typer.echo("✗ Manifest missing required field: 'image_url'", err=True)
            return None
        
        typer.echo(f"✓ Manifest loaded successfully")
        typer.echo(f"  XML URL: {manifest['xml_url']}")
        typer.echo(f"  Image URL: {manifest['image_url']}")
        
        return manifest
        
    except requests.exceptions.RequestException as e:
        typer.echo(f"✗ Failed to download manifest: {e}", err=True)
        return None
    except json.JSONDecodeError as e:
        typer.echo(f"✗ Failed to parse manifest JSON: {e}", err=True)
        return None
    except Exception as e:
        typer.echo(f"✗ Error loading manifest: {e}", err=True)
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
    manifest_url: str = typer.Option(..., "--url", help="URL of the JSON manifest file"),
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
    Import and initialize a VM from a JSON manifest.
    
    The manifest is a JSON file containing URLs for the VM XML definition and disk image:
    {
        "xml_url": "http://example.com/vm-definition.xml",
        "image_url": "http://example.com/disk-image.qcow2"
    }
    
    This command downloads the manifest, fetches both the XML and image,
    edits the XML to use the new VM name and disk path, and defines the VM in libvirt.
    
    Examples:
    
        # Import from a manifest
        boxman-import-vm --url http://example.com/manifest.json --name my-ubuntu-vm
        
        # Import with custom disk directory
        boxman-import-vm --url http://example.com/manifest.json --name my-vm --disk-dir /var/lib/libvirt/images
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
    
    # Download and parse the manifest
    typer.echo(f"\n[1/5] Loading manifest...")
    manifest = download_manifest(manifest_url)
    if manifest is None:
        typer.echo("✗ Failed to load manifest", err=True)
        raise typer.Exit(code=1)
    
    xml_url = manifest['xml_url']
    image_url = manifest['image_url']
    
    # Determine disk directory
    if disk_dir is None:
        disk_dir = os.getcwd()
    disk_dir = os.path.abspath(os.path.expanduser(disk_dir))
    
    # Create disk directory if it doesn't exist
    os.makedirs(disk_dir, exist_ok=True)
    
    # Determine disk filename - extract extension from image URL if possible
    parsed_url = urlparse(image_url)
    url_path = parsed_url.path
    url_extension = os.path.splitext(url_path)[1].lower() if url_path else ''
    
    # Use .qcow2 as default, but respect the URL extension if it looks valid
    if url_extension in ['.qcow2', '.qcow', '.img', '.raw']:
        disk_filename = f"{vm_name}{url_extension}"
    else:
        disk_filename = f"{vm_name}.qcow2"
        typer.echo(f"⚠ Could not determine image format from URL, using .qcow2 extension")
    
    disk_path = os.path.join(disk_dir, disk_filename)
    
    # Download the VM XML definition
    typer.echo(f"\n[2/5] Downloading XML definition...")
    temp_xml_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            temp_xml_file = f.name
        
        if not download_image(xml_url, temp_xml_file):
            typer.echo("✗ Failed to download XML definition", err=True)
            raise typer.Exit(code=1)
        
        typer.echo(f"✓ XML definition downloaded")
    except Exception as e:
        typer.echo(f"✗ Error downloading XML: {e}", err=True)
        if temp_xml_file and os.path.exists(temp_xml_file):
            os.unlink(temp_xml_file)
        raise typer.Exit(code=1)
    
    # Download the disk image
    typer.echo(f"\n[3/5] Downloading disk image...")
    if not download_image(image_url, disk_path):
        typer.echo("✗ Failed to download disk image", err=True)
        if temp_xml_file and os.path.exists(temp_xml_file):
            os.unlink(temp_xml_file)
        raise typer.Exit(code=1)
    
    try:
        # Edit the XML
        typer.echo(f"\n[4/5] Editing VM configuration...")
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
        # Clean up temporary XML file
        if temp_xml_file and os.path.exists(temp_xml_file):
            os.unlink(temp_xml_file)


def main():
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
