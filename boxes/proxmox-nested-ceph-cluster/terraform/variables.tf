variable "pve_endpoint" {
  description = "Proxmox API URL. Through an ssh tunnel: ssh -L 8006:10.77.0.11:8006 hpe1"
  type        = string
  default     = "https://localhost:8006/"
}

variable "pve_api_token" {
  description = "API token as 'user@realm!name=secret' (make tf-token writes it to terraform/.env)"
  type        = string
  sensitive   = true
}

variable "nodes" {
  description = "Cluster nodes that receive VMs, round-robin; each downloads its own copy of the image"
  type        = list(string)
  default     = ["pve1", "pve2", "pve3", "pve4"]
}

variable "vm_count" {
  description = "How many Rocky 9 VMs to create"
  type        = number
  default     = 4
}

variable "vm_name_prefix" {
  type    = string
  default = "rocky"
}

variable "vm_id_base" {
  description = "First VM id; the box's demo VM is 100, so start above it"
  type        = number
  default     = 200
}

variable "cores" {
  type    = number
  default = 2
}

variable "memory_mb" {
  type    = number
  default = 2048
}

variable "disk_gb" {
  description = "Root disk size; the image is grown to this on import"
  type        = number
  default     = 16
}

variable "datastore" {
  description = "Where the root disk and the cloud-init drive live; Ceph keeps the VMs migratable"
  type        = string
  default     = "vmpool"
}

variable "image_datastore" {
  description = "Per-node dir storage holding the downloaded image (content type import)"
  type        = string
  default     = "local"
}

variable "image_url" {
  type    = string
  default = "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2"
}

variable "image_file_name" {
  description = "Name under <image_datastore>:import/; must end in .qcow2 (or .raw/.vmdk)"
  type        = string
  default     = "rocky-9-genericcloud.qcow2"
}

variable "ssh_public_key" {
  description = "Public key injected for the cloud-init user (default: the box's lab key)"
  type        = string
  default     = ""
}

variable "cloud_user" {
  type    = string
  default = "rocky"
}

variable "bridge" {
  type    = string
  default = "vmbr0"
}
