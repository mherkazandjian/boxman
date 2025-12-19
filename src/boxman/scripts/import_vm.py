#!/usr/bin/env python
"""
Utility script for importing/initializing VM images from URLs.

This script downloads a qcow2 image from a URL (HTTP, Google Drive, or OneDrive),
edits the VM XML definition to use the new image and name, and defines the VM in libvirt.
"""

import os
import sys
import uuid
import tempfile
import traceback
from pathlib import Path
from typing import Optional
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
        # OneDrive URL handling is complex and may not work for all share link formats
        # Try a basic approach for common OneDrive patterns
        download_url = url
        
        # Attempt to construct direct download URL for onedrive.live.com links
        if 'onedrive.live.com' in url and 'download' not in url:
            # Try appending download parameter
            separator = '&' if '?' in url else '?'
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
    # Detect URL type and use appropriate download method
    if 'drive.google.com' in url or 'docs.google.com' in url:
        return download_file_google_drive(url, dest_path)
    elif 'onedrive.live.com' in url or '1drv.ms' in url or 'sharepoint.com' in url:
        return download_file_onedrive(url, dest_path)
    else:
        return download_file_http(url, dest_path)


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


def get_template_vm_xml(template_vm: str, uri: str = "qemu:///system") -> Optional[str]:
    """
    Dump the XML of a template VM.
    
    Args:
        template_vm: The name of the template VM
        uri: The libvirt URI (default: qemu:///system)
        
    Returns:
        The XML string if successful, None otherwise
    """
    try:
        typer.echo(f"Dumping XML from template VM: {template_vm}")
        
        result = run(
            f"virsh -c {uri} dumpxml {template_vm}",
            hide=True,
            warn=True
        )
        
        if result.ok:
            typer.echo(f"✓ Template VM XML retrieved")
            return result.stdout
        else:
            typer.echo(f"✗ Failed to get template VM XML: {result.stderr}", err=True)
            return None
            
    except Exception as e:
        typer.echo(f"✗ Error getting template VM XML: {e}", err=True)
        return None


@app.command()
def import_image(
    image_url: str = typer.Argument(..., help="URL of the qcow2 image to download"),
    vm_name: str = typer.Argument(..., help="Name for the new VM"),
    disk_dir: str = typer.Option(
        None,
        "--disk-dir",
        "-d",
        help="Directory to save the disk image (default: current directory)"
    ),
    xml_template: Optional[str] = typer.Option(
        None,
        "--xml-template",
        "-x",
        help="Path to XML template file or name of existing VM to use as template"
    ),
    template_vm: Optional[str] = typer.Option(
        None,
        "--template-vm",
        "-t",
        help="Name of an existing VM to use as a template (alternative to --xml-template)"
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
    Import and initialize a VM from a qcow2 image URL.
    
    This command downloads a qcow2 image from a URL, edits a VM XML template to use
    the new image and name, and defines the VM in libvirt.
    
    Examples:
    
        # Import with XML template file
        boxman-import-vm http://example.com/ubuntu.qcow2 my-ubuntu-vm --xml-template template.xml
        
        # Import using an existing VM as template
        boxman-import-vm http://example.com/ubuntu.qcow2 my-ubuntu-vm --template-vm base-template
        
        # Import from Google Drive
        boxman-import-vm https://drive.google.com/file/d/xxxxx/view my-vm --template-vm base
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
    
    # Determine disk directory
    if disk_dir is None:
        disk_dir = os.getcwd()
    disk_dir = os.path.abspath(os.path.expanduser(disk_dir))
    
    # Create disk directory if it doesn't exist
    os.makedirs(disk_dir, exist_ok=True)
    
    # Determine disk filename - extract extension from URL if possible
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
    
    # Download the image
    typer.echo(f"\n[1/4] Downloading image...")
    if not download_image(image_url, disk_path):
        typer.echo("✗ Failed to download image", err=True)
        raise typer.Exit(code=1)
    
    # Get or create XML template
    typer.echo(f"\n[2/4] Preparing XML template...")
    
    xml_content = None
    temp_xml_file = None
    
    if template_vm:
        # Use existing VM as template
        xml_content = get_template_vm_xml(template_vm, uri)
        if xml_content is None:
            typer.echo("✗ Failed to get template VM XML", err=True)
            raise typer.Exit(code=1)
    elif xml_template:
        # Use XML template file
        xml_template_path = os.path.abspath(os.path.expanduser(xml_template))
        if not os.path.exists(xml_template_path):
            typer.echo(f"✗ XML template file not found: {xml_template_path}", err=True)
            raise typer.Exit(code=1)
        with open(xml_template_path, 'r') as f:
            xml_content = f.read()
        typer.echo(f"✓ Using XML template: {xml_template_path}")
    else:
        typer.echo("✗ Either --xml-template or --template-vm must be specified", err=True)
        raise typer.Exit(code=1)
    
    # Create a temporary XML file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
        f.write(xml_content)
        temp_xml_file = f.name
    
    try:
        # Edit the XML
        typer.echo(f"\n[3/4] Editing VM configuration...")
        if not edit_vm_xml(temp_xml_file, vm_name, disk_path, change_uuid=not keep_uuid):
            typer.echo("✗ Failed to edit XML", err=True)
            raise typer.Exit(code=1)
        
        # Define the VM
        typer.echo(f"\n[4/4] Defining VM in libvirt...")
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
