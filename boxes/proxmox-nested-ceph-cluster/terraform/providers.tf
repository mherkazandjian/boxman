# API-only: no `ssh {}` block is needed because the image is downloaded by the
# node itself (download_file) and imported with `import_from`, and cloud-init
# uses the built-in user_account/ip_config rather than snippets.
provider "proxmox" {
  endpoint  = var.pve_endpoint
  api_token = var.pve_api_token
  insecure  = true # self-signed lab certificate
}
