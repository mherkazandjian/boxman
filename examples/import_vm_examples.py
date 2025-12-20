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
    
    # Example 2: Basic usage with package
    print("\n## Example 2: Import from .tar.gz package")
    print("# This imports a VM from a package containing manifest, XML, and disk image")
    print("# Command (not executed):")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/ubuntu-vm.tar.gz \\")
    print("    --name my-ubuntu-vm")
    
    # Example 3: With custom disk directory
    print("\n## Example 3: Import with custom disk directory")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/centos-vm.tar.gz \\")
    print("    --name my-centos \\")
    print("    --disk-dir /var/lib/libvirt/images")
    
    # Example 4: Force overwrite
    print("\n## Example 4: Force overwrite existing VM")
    print("# Use --force to replace an existing VM:")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/updated-vm.tar.gz \\")
    print("    --name existing-vm \\")
    print("    --force")
    
    # Example 5: Package structure
    print("\n## Example 5: Creating a VM Package")
    print("# 1. Create a manifest.json:")
    print("""  {
    "xml_path": "vm-definition.xml",
    "image_path": "disk-image.qcow2"
  }""")
    print("\n# 2. Package the files:")
    print("  tar -czf my-vm.tar.gz manifest.json vm-definition.xml disk-image.qcow2")
    print("\n# 3. Upload and share the package URL")
    
    print("\n" + "=" * 70)
    print("For more information, see docs/import-vm-utility.md")
    print("=" * 70)

if __name__ == "__main__":
    main()
