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
  description = "ready-made ssh commands (run from hpe1, which sits on the 10.77.0.0/24 L2)"
  value = [
    for name, vm in proxmox_virtual_environment_vm.rocky :
    format("ssh -i ~/pve-lab/keys/id_ed25519_pvelab rocky@%s   # %s on %s",
      try([for ip in flatten(vm.ipv4_addresses) : ip if ip != "127.0.0.1"][0], "?"), name, vm.node_name)
  ]
}
