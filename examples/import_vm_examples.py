#!/usr/bin/env python3
"""
Example usage of boxman-import-vm utility

This script demonstrates various ways to use the VM import utility with JSON manifests.
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
    
    # Example 2: Basic usage with manifest
    print("\n## Example 2: Import from JSON manifest")
    print("# This imports a VM from a manifest containing XML and image URLs")
    print("# Command (not executed):")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/ubuntu-manifest.json \\")
    print("    --name my-ubuntu-vm")
    
    # Example 3: With custom disk directory
    print("\n## Example 3: Import with custom disk directory")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/centos-manifest.json \\")
    print("    --name my-centos \\")
    print("    --disk-dir /var/lib/libvirt/images")
    
    # Example 4: From Google Drive
    print("\n## Example 4: Import manifest from Google Drive")
    print("# The manifest itself can be hosted on Google Drive:")
    print("  boxman-import-vm \\")
    print("    --url 'https://drive.google.com/file/d/MANIFEST_ID/view' \\")
    print("    --name windows-10")
    
    # Example 5: Force overwrite
    print("\n## Example 5: Force overwrite existing VM")
    print("# Use --force to replace an existing VM:")
    print("  boxman-import-vm \\")
    print("    --url http://example.com/updated-manifest.json \\")
    print("    --name existing-vm \\")
    print("    --force")
    
    # Example 6: Manifest format
    print("\n## Example 6: JSON Manifest Format")
    print("# Create a manifest.json file with this structure:")
    print("""  {
    "xml_url": "http://example.com/vm-definition.xml",
    "image_url": "http://example.com/disk-image.qcow2"
  }""")
    
    print("\n" + "=" * 70)
    print("For more information, see docs/import-vm-utility.md")
    print("=" * 70)

if __name__ == "__main__":
    main()
