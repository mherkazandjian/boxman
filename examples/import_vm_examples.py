#!/usr/bin/env python3
"""
Example usage of boxman-import-vm utility

This script demonstrates various ways to use the VM import utility.
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
    
    # Example 2: Basic usage with template VM
    print("\n## Example 2: Import from HTTP URL with template VM")
    print("# This would import a VM image and use 'ubuntu-base' as template")
    print("# Command (not executed):")
    print("  boxman-import-vm \\")
    print("    http://cloud-images.ubuntu.com/releases/24.04/ubuntu-24.04.qcow2 \\")
    print("    my-ubuntu-vm \\")
    print("    --template-vm ubuntu-base \\")
    print("    --disk-dir /var/lib/libvirt/images")
    
    # Example 3: Using XML template
    print("\n## Example 3: Import with XML template file")
    print("# First export a template:")
    print("  virsh -c qemu:///system dumpxml base-vm > /tmp/template.xml")
    print()
    print("# Then import:")
    print("  boxman-import-vm \\")
    print("    http://example.com/centos.qcow2 \\")
    print("    my-centos \\")
    print("    --xml-template /tmp/template.xml \\")
    print("    --disk-dir /var/lib/libvirt/images")
    
    # Example 4: Google Drive
    print("\n## Example 4: Import from Google Drive")
    print("# For large files on Google Drive:")
    print("  boxman-import-vm \\")
    print("    'https://drive.google.com/file/d/FILE_ID/view' \\")
    print("    windows-10 \\")
    print("    --template-vm windows-base \\")
    print("    --disk-dir /data/vms")
    
    # Example 5: Force overwrite
    print("\n## Example 5: Force overwrite existing VM")
    print("# Use --force to replace an existing VM:")
    print("  boxman-import-vm \\")
    print("    http://example.com/updated.qcow2 \\")
    print("    existing-vm \\")
    print("    --template-vm base \\")
    print("    --force")
    
    print("\n" + "=" * 70)
    print("For more information, see docs/import-vm-utility.md")
    print("=" * 70)

if __name__ == "__main__":
    main()
