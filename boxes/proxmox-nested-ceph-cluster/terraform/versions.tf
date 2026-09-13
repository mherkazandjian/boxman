terraform {
  required_version = ">= 1.5"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # The old floor (>= 0.70.0, with a comment claiming 0.66) was below
      # what this configuration needs: disk.import_from and the `import`
      # content type arrived in 0.79, the short resource names in 0.100, and
      # HA `failback` in 0.107. Pinned to the ~> 0.112 line that the lab was
      # actually built and verified against. `~> 0.112` would admit any later 0.x;
      # `~> 0.112.0` pins the 0.112.x series, which is the intent (#171 B21).
      version = "~> 0.112.0"
    }
  }
}
