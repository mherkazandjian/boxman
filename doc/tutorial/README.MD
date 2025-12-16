# Chapter 1 – Getting Started with Boxman on Libvirt

**Target audience:**  
Linux users and junior sysadmins with basic familiarity with virtualization, SSH, and configuration management.

**Hardware assumptions:**  
- Laptop or workstation
- 8 GB RAM  
- ~50 GB free disk space  


---

## 1. Introduction

In this chapter, we will:

- Install and configure **libvirt** with QEMU
- Prepare a reusable base VM image
- Install and verify **Boxman**
- Provision a **one-machine cluster**
- Run a **simple Ansible playbook** against the provisioned VM

The end goal is a reproducible local lab environment that runs comfortably on modest hardware.

---

## 2. Demo (Screen Recording)

> **Demo idea (to be recorded):**
>
> - Show libvirt running (`virsh list`)
> - Provision a VM with Boxman
> - SSH into the VM
> - Run the Ansible playbook successfully
>
> Keep the demo short (3–5 minutes) and focus on flow rather than internals.

---

## 3. Configuration at a Glance

Here is the minimal Boxman configuration that we will be using to spin up a single-machine cluster:

```yaml
---
version: 1.0
project: myproject1
provider:
  libvirt:
    uri: qemu:///system
    use_sudo: True
    virt_install_cmd: '/usr/bin/python /usr/bin/virt-install'
    virt_clone_cmd: '/usr/bin/python /usr/bin/virt-clone'
    virsh_cmd: '/bin/virsh'
  verbose: True

clusters:
  cluster_1:
    workdir: ~/workspaces/myproject1
    base_image: rocky9
    proxy_host: localhost
    admin_user: 'root'
    admin_pass: 'file://~/workspaces/myproject1/admin_pass.txt'
    admin_key_name: id_ed25519_boxman
    ssh_config: ssh_config

    networks:
      default_network:
        network: 192.168.10.0/24
        enable: True
        autostart: True

    vms:
      boxman01:
        hostname: boxman01
        disks:
          - name: disk01
            driver:
              name: qemu
              type: qcow2
            target: vdb
            size: 4096
        cpus:
          sockets: 1
          cores: 1
          threads: 1
        memory: 2048
        network_adapters:
          - name: adapter_1
            link_state: 'up'
            network_source: 'default_network'
    files:
      hello.txt: |
        Hello, Boxman!
      script.sh: |
        #!/bin/bash
        echo "This is a script inside the VM."
        date
```

Don’t worry if this looks dense—we’ll revisit the important parts as we go.

---

## 4. Installing Libvirt and Dependencies

Libvirt is the virtualization layer used by Boxman.
Official documentation is available here:
👉 [https://libvirt.org/compiling.html](https://libvirt.org/compiling.html)

Below are **distribution-specific shortcuts**.

### Arch Linux

```bash
sudo pacman -S libvirt qemu virt-install virt-clone
```

Or with AUR helpers:

```bash
yay -S libvirt
```

Enable and start libvirt:

```bash
sudo systemctl enable --now libvirtd
```

Add your user to the libvirt group:

```bash
sudo usermod -aG libvirt $USER
```

Log out and back in afterward.

---

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y libvirt-daemon-system libvirt-clients qemu-kvm virtinst
```

Enable libvirt:

```bash
sudo systemctl enable --now libvirtd
```

Add your user to required groups:

```bash
sudo usermod -aG libvirt,kvm $USER
```

Note: You should restart your session for changes to take effect. (Log out then log back in)

---

### CentOS / Rocky / RHEL

```bash
sudo yum install -y libvirt libvirt-client qemu-kvm virt-install virt-clone
```

Enable libvirt:

```bash
sudo systemctl enable --now libvirtd
```

---

### Validation

Ensure libvirt is reachable via the system socket:

```bash
virsh -c qemu:///system list

 Id   Name     State
------------------------
 1    rocky9   running
```

If this works, libvirt is correctly installed.

---

## 5. Consolidating the Image Location

For simplicity and consistency, keep all base images in a single location, for example:

```bash
/var/lib/libvirt/images/
```

Make sure your user (or root) has read access , and that Boxman knows the image name (e.g. `rocky9`).

It is recommended to store the Disk Images somewhere easily accessible by the user(e.g. `~/boxman_vms`).

---

## 6. Creating a Base Image for Boxman

Boxman expects a **pre-installed VM image**.

Steps:

1. Manually create a VM using `virt-install` or `virt-manager`
2. Install the OS from an ISO (e.g. Rocky Linux 9)
3. **Important:**

   * Create a user you will manage the VM with
   * Set a known password *or* prepare SSH access
4. Shut down the VM after installation
5. Keep the resulting disk image (`qcow2` or `vhd`) as your base image

This base image will be cloned for every Boxman-managed VM.

---

## 7. Installing Boxman

### Python Requirements

* **Python 3.12 supported**
* **Python 3.13 is not supported**
* You may need `pyenv`, `asdf`, or similar tools

Example with a virtual environment:

```bash
python3.12 -m boxman boxman_env
source boxman_env/bin/activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

Ensure the following commands are available:

* `virt-install`
* `virt-clone`
* `virsh`

And that libvirt is accessible via:

```text
qemu:///system
```

### Install Boxman

```bash
python setup.py install
```

Verify:

```bash
boxman --version
```

---

## 8. Spinning Up a One-Machine Cluster

1. Save the configuration shown earlier as `conf.yml`
2. Run:

```bash
boxman --conf /path/to/conf.yml provision
```

Provisioning will take a few minutes.
When complete, you should have:

* One VM running
* Networking configured
* SSH keys generated automatically
* Files injected into the VM

Congratulations—you now have a **single-node Boxman cluster**.

---

## 9. Running a Simple Ansible Playbook

Now we’ll validate the VM using Ansible.

### Playbook

```yaml
- name: Simple play to verify host, install a package and update a config line
  hosts: default
  become: true
  gather_facts: false

  vars:
    package_name: htop
    config_file: /etc/myapp.conf
    config_regexp: '^option='
    config_line: 'option=true'

  tasks:
    - name: Ping the host
      ping:

    - name: Install a package
      package:
        name: "{{ package_name }}"
        state: present

    - name: Ensure configuration line is present
      lineinfile:
        path: "{{ config_file }}"
        regexp: "{{ config_regexp }}"
        line: "{{ config_line }}"
        create: yes
        backup: yes
```

### What This Does

1. **Ping the host** – confirms connectivity
2. **Install a package** – verifies package management works
3. **Modify a config file** – tests idempotent configuration changes

If this playbook succeeds, your Boxman-managed VM is fully functional.

---

## 10. Summary

By the end of this chapter, you have:

* Installed and configured libvirt
* Created a reusable base image
* Installed and verified Boxman
* Provisioned a one-node cluster
* Successfully managed it using Ansible

This setup forms a solid foundation for expanding into multi-node clusters and more advanced automation in later chapters.

```
```
