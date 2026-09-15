output "vms" {
  description = "name -> node / vmid / first IPv4 reported by the guest agent"
  value = {
    for name, vm in proxmox_virtual_environment_vm.rocky :
    name => {
      node = vm.node_name
      vmid = vm.vm_id
      ipv4 = try([for ip in flatten(vm.ipv4_addresses) : ip if ip != "127.0.0.1"][0], null)
    }
  }
}

output "ssh" {
  description = "ready-made ssh commands (run from host1, which sits on the 10.77.0.0/24 L2)"
  value = [
    for name, vm in proxmox_virtual_environment_vm.rocky :
    # var.cloud_user, not a hardcoded "rocky": the two disagree the moment
    # anyone sets the variable (#171 B23)
    format("ssh -i ~/pve-lab/keys/id_ed25519_pvelab %s@%s   # %s on %s", var.cloud_user,
    try([for ip in flatten(vm.ipv4_addresses) : ip if ip != "127.0.0.1"][0], "?"), name, vm.node_name)
  ]
}

output "ha_policy_homes" {
  description = <<-EOT
    Configured policy home per HA resource: `vm:<id>` -> the node that
    `node_overrides`, or the round-robin over `nodes`, placed it on.

    Deliberately derived from local.vms and NOT from `node_name` in state.
    `ignore_changes` permits the HA manager to move a VM between nodes, so
    refreshed state records wherever it ended up; feeding that back would make
    the affinity rule follow the drift the rule exists to correct.

    `scripts/pve-ha.sh` requires this, with no fallback. Changing
    `node_overrides`, `nodes` or `vm_id_base` therefore changes the affinity
    rules the next time `make ha` runs.
  EOT
  value       = { for name, v in local.vms : "vm:${v.vm_id}" => v.node }
}
