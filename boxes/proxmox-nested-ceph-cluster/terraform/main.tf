# Rocky 9 VMs on the nested Proxmox cluster.
#
#   node (per node)  : downloads the GenericCloud qcow2 into local:import/
#   each VM          : root disk imported from that file onto Ceph (vmpool),
#                      cloud-init drive on Ceph, DHCP from hpe1's dnsmasq via
#                      vmbr0 (reservation range .100-.199), lab ssh key,
#                      qemu-guest-agent reports the address back.
#
# Placement is round-robin over var.nodes, so `vm_count = 4` gives one VM per
# node, two per physical host.

locals {
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : trimspace(file("${path.module}/../keys/id_ed25519_pvelab.pub"))

  vms = {
    for i in range(var.vm_count) :
    format("%s%02d", var.vm_name_prefix, i + 1) => {
      vm_id = var.vm_id_base + i
      node = lookup(var.node_overrides, format("%s%02d", var.vm_name_prefix, i + 1),
      var.nodes[i % length(var.nodes)])
    }
  }

  # nodes that actually host a VM (no point downloading the image elsewhere)
  image_nodes = toset([for v in local.vms : v.node])
}

resource "proxmox_download_file" "rocky9" {
  for_each = local.image_nodes

  node_name      = each.value
  content_type   = "import"
  datastore_id   = var.image_datastore
  file_name      = var.image_file_name
  url            = var.image_url
  overwrite      = false
  upload_timeout = 1800
}

resource "proxmox_virtual_environment_vm" "rocky" {
  for_each = local.vms

  name        = each.key
  description = "Rocky 9 from GenericCloud, managed by Terraform"
  tags        = ["terraform", "rocky9"]
  node_name   = each.value.node
  vm_id       = each.value.vm_id

  started         = true
  on_boot         = true
  stop_on_destroy = true
  migrate         = true # a changed node_name is a (live) migration, never a re-create

  agent {
    enabled = true
    timeout = "10m"
  }

  cpu {
    cores = var.cores
    type  = "x86-64-v2-AES"
  }

  memory {
    dedicated = var.memory_mb
  }

  scsi_hardware = "virtio-scsi-single"
  boot_order    = ["scsi0"]

  disk {
    datastore_id = var.datastore
    import_from  = proxmox_download_file.rocky9[each.value.node].id
    interface    = "scsi0"
    size         = var.disk_gb
    discard      = "on"
    iothread     = true
  }

  initialization {
    datastore_id = var.datastore

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }

    user_account {
      username = var.cloud_user
      keys     = [local.ssh_public_key]
    }
  }

  network_device {
    bridge = var.bridge
    model  = "virtio"
    mtu    = 1 # inherit the bridge MTU (1450 across the VXLAN)
  }

  operating_system {
    type = "l26"
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  lifecycle {
    # - the imported disk keeps its source reference only at creation time
    # - Proxmox owns *where* a VM runs (HA recovery, node affinity, dynamic
    #   CRS rebalancing) and whether HA has it started, so node_name is only
    #   the initial placement and later moves are not drift
    ignore_changes = [disk[0].import_from, node_name, started]
  }
}
