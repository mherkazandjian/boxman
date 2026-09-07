terraform {
  required_version = ">= 1.5"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = ">= 0.70.0" # import_from + `import` content type need >= 0.66
    }
  }
}
