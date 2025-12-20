#!/usr/bin/env python3
"""
Example usage of boxman-import-vm utility

This script demonstrates various ways to use the VM import utility with .tar.gz packages.
"""

import subprocess
import sys

def run_command(cmd):
    """Run a command and print it first"""
    print(f"\n$ {cmd}")
    print("-" * 70)
    result = subprocess.run(cmd, shell=True, capture_output=False)
    return result.returncode == 0

def main():
    print("=" * 70)
    print("boxman-import-vm - Example Usage")
    print("=" * 70)
    
    # Example 1: Show help
    print("\n## Example 1: Display help")
    run_command("boxman-import-vm --help")
    
    # Example 2: Basic usage with HTTP URL
    print("\n## Example 2: Import from HTTP URL")
    print("# This imports a VM from a package on a web server")
    print("# Command (not executed):")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/ubuntu-vm.tar.gz \\")
    print("    --name my-ubuntu-vm")
    
    # Example 3: Google Drive
    print("\n## Example 3: Import from Google Drive")
    print("# For packages hosted on Google Drive:")
    print("  boxman-import-vm \\")
    print("    --url https://drive.google.com/file/d/1SEcpVIrh5yc/view \\")
    print("    --name my-gdrive-vm")
    
    # Example 4: Local file
    print("\n## Example 4: Import from local file")
    print("# Using file:// URL for local filesystem:")
    print("  boxman-import-vm \\")
    print("    --url file:///var/backups/vm-package.tar.gz \\")
    print("    --name restored-vm")
    
    # Example 5: With custom disk directory
    print("\n## Example 5: Import with custom disk directory")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/centos-vm.tar.gz \\")
    print("    --name my-centos \\")
    print("    --disk-dir /var/lib/libvirt/images")
    
    # Example 6: Force overwrite
    print("\n## Example 6: Force overwrite existing VM")
    print("# Use --force to replace an existing VM:")
    print("  boxman-import-vm \\")
    print("    --url file:///tmp/updated-vm.tar.gz \\")
    print("    --name existing-vm \\")
    print("    --force")
    
    # Example 7: Package structure
    print("\n## Example 7: Creating a VM Package")
    print("# 1. Create a manifest.json:")
    print("""  {
    "xml_path": "vm-definition.xml",
    "image_path": "disk-image.qcow2"
  }""")
    print("\n# 2. Package the files:")
    print("  tar -czf my-vm.tar.gz manifest.json vm-definition.xml disk-image.qcow2")
    print("\n# 3. Upload to a web server, Google Drive, or use locally with file:// URL")
    
    print("\n" + "=" * 70)
    print("For more information, see docs/import-vm-utility.md")
    print("=" * 70)

if __name__ == "__main__":
    main()
