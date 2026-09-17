# Every VM is an HA resource: the HA manager restarts it elsewhere when its
# node dies, keeps it `started`, and (with the cluster's dynamic CRS) may
# live-migrate it to balance load. Placement policy — node-affinity rules and
# the CRS settings — is applied by scripts/pve-ha.sh, because the provider has
# no resource for HA rules yet; the VM resource ignores node_name/started so
# those moves never show up as Terraform drift.
resource "proxmox_virtual_environment_haresource" "rocky" {
  for_each = var.ha_enabled ? proxmox_virtual_environment_vm.rocky : {}

  resource_id  = "vm:${each.value.vm_id}"
  state        = "started"
  failback     = true # move back when a higher-priority node (per affinity rule) returns
  max_restart  = 1
  max_relocate = 1
  comment      = "Managed by Terraform (${each.key})"
}
