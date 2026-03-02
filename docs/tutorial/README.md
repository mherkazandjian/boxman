# Getting Started with Boxman

> **Your first local VM cluster — from zero to SSH in under 30 minutes.**

Boxman is an infrastructure-as-code tool for managing local virtual-machine clusters on top of **libvirt/QEMU**. Think of it as a "Docker Compose for VMs" — you describe your machines, networks, and disks in a single YAML file, and Boxman provisions everything for you.

---

## Table of Contents

1. [What You'll Need](#1-what-youll-need)
2. [Install Libvirt (the Virtualization Layer)](#2-install-libvirt-the-virtualization-layer)
3. [Create a Base VM Image](#3-create-a-base-vm-image)
4. [Install Boxman](#4-install-boxman)
5. [Understand the Configuration File](#5-understand-the-configuration-file)
6. [Build Your First Config from Scratch](#6-build-your-first-config-from-scratch)
7. [Provision Your Cluster](#7-provision-your-cluster)
8. [Connect to Your VM](#8-connect-to-your-vm)
9. [Day-to-Day Commands](#9-day-to-day-commands)
10. [Going Further — Multi-VM Cluster](#10-going-further--multi-vm-cluster)
11. [Advanced Features](#11-advanced-features)
12. [Verify with Ansible (Optional)](#12-verify-with-ansible-optional)
13. [Troubleshooting](#13-troubleshooting)
14. [Quick Reference](#14-quick-reference)

---

## 1. What You'll Need

| Requirement | Details |
|---|---|
| **Operating System** | Linux (any distro with libvirt support) |
| **RAM** | 8 GB minimum (each VM will use a share of this) |
| **Disk** | ~50 GB free space |
| **Python** | **3.12** (3.13 is not yet supported) |
| **System tools** | `virt-install`, `virt-clone`, `virsh`, `sshpass`, `qemu-img` |
| **Permissions** | Your user must be in the `libvirt` (and `kvm`) group |

> 💡 **Why Python 3.12?** If your distro ships a different version, use
> [pyenv](https://github.com/pyenv/pyenv) or [asdf](https://asdf-vm.com/) or
> [conda](https://github.com/conda/conda) to install 3.12 alongside your system 
> Python.

---

## 2. Install Libvirt (the Virtualization Layer)

Libvirt is the virtualization API that sits between Boxman and QEMU/KVM.
Official docs: [libvirt.org](https://libvirt.org/compiling.html)

Pick your distribution below and run the commands in a terminal.

### Arch Linux

```bash
sudo pacman -S libvirt qemu-full virt-install virt-clone sshpass
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt $USER
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y libvirt-daemon-system libvirt-clients qemu-kvm virtinst sshpass
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm $USER
```

### CentOS / Rocky / RHEL

```bash
sudo dnf install -y libvirt libvirt-client qemu-kvm virt-install virt-clone sshpass
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt $USER
```

> ⚠️ **After adding yourself to the group, log out and log back in** for the
> change to take effect.

### Verify the installation

```bash
virsh -c qemu:///system list
```

You should see an empty table (or existing VMs). If you get a connection error,
libvirt is not running — check `systemctl status libvirtd`.

---

## 3. Create a Base VM Image

Boxman does **not** install an OS from scratch. Instead, it **clones** a
pre-existing VM (the "base image") for every VM it creates. You need to prepare
this base image once.

### Step-by-step

1. **Download an ISO** — for example,
   [Rocky Linux 9](https://rockylinux.org/download) minimal.

2. **Create a VM manually** using `virt-manager` (GUI) or `virt-install` (CLI):

   ```bash
   sudo virt-install \
     --name rocky9 \
     --ram 2048 \
     --vcpus 2 \
     --disk path=/var/lib/libvirt/images/rocky9.qcow2,size=20 \
     --os-variant rocky9 \
     --cdrom ~/Downloads/Rocky-9-latest-x86_64-minimal.iso \
     --network network=default \
     --graphics vnc
   ```

3. **Complete the OS installation** through the graphical console
   (`virt-manager` → double-click the VM).

4. **During installation, set up:**
   - A root password you will remember (Boxman needs it for initial SSH key
     setup)
   - Enable SSH (`sshd`)

5. **Shut down the VM** after installation:

   ```bash
   virsh -c qemu:///system shutdown rocky9
   ```

6. **Leave the VM defined** (do not `undefine` it). Boxman will reference it by
   name — in this example, `rocky9`.

> 💡 **Tip:** Store your disk images in `/var/lib/libvirt/images/` (the libvirt
> default) or a custom path like `~/boxman_images/`. Either works as long as
> libvirt has read access.

---

## 4. Install Boxman

### Set up a Python virtual environment

```bash
python3.12 -m venv boxman_env
source boxman_env/bin/activate
```

### Install dependencies and Boxman

```bash
pip install -r requirements.txt
python setup.py install
```

### Verify

```bash
boxman --version
```

You should see `0.9.0dev` (or the current version). If the command is not found,
make sure your virtual environment is active.

---

## 5. Understand the Configuration File

Before building a config, let's understand every section. Boxman uses a single
YAML file to describe your entire environment.

> 💡 **The config file is also a Jinja2 template** — it is rendered before being
> parsed. This means you can use variables, loops, and conditionals right inside
> your YAML (covered in [Advanced Features](#11-advanced-features)).

### 5.1 Top-level structure

Every config file has three required top-level keys:

```yaml
version: 1.0          # Config format version (always 1.0 for now)
project: myproject     # A unique name for this project (used in VM and network naming)
provider:              # Which virtualization backend to use
  libvirt: { ... }
clusters:              # One or more clusters to manage
  my_cluster: { ... }
```

### 5.2 Provider block

Tells Boxman how to talk to libvirt:

```yaml
provider:
  libvirt:
    uri: qemu:///system                                       # Libvirt connection URI
    use_sudo: True                                            # Run virsh/virt-* commands with sudo
    virt_install_cmd: '/usr/bin/python /usr/bin/virt-install'  # Full path to virt-install
    virt_clone_cmd: '/usr/bin/python /usr/bin/virt-clone'      # Full path to virt-clone
    virsh_cmd: '/bin/virsh'                                    # Full path to virsh
  verbose: True                                                # Enable detailed log output
```

> 💡 **Finding your paths:** Run `which virt-install`, `which virt-clone`, and
> `which virsh` to find the correct paths for your system.

### 5.3 Cluster block

A cluster groups related VMs, networks, and files together:

```yaml
clusters:
  cluster_1:
    workdir: ~/workspaces/myproject       # Where Boxman stores generated files (SSH keys, configs)
    base_image: rocky9                    # Name of the libvirt VM to clone (your base image)
    proxy_host: localhost                 # Proxy host (use localhost for local setups)
    admin_user: 'root'                   # SSH user on the base image
    admin_pass: 'mypassword'             # Password for initial SSH key injection (see below)
    admin_key_name: id_ed25519_boxman    # Name of the SSH keypair (auto-generated)
    ssh_config: ssh_config               # SSH config filename (auto-generated)
```

#### How `admin_pass` works

The password is only used **once** during provisioning — to copy an SSH public
key into the VM via `ssh-copy-id`. After that, all access is key-based. You can
provide the password in three ways:

| Format | Example | Description |
|---|---|---|
| Plain text | `'mysecretpass'` | Written directly in the config (simple but less secure) |
| From a file | `'file://~/secrets/pass.txt'` | Read from a file at provisioning time |
| Environment variable | `'${env:BOXMAN_ADMIN_PASS}'` | Read from an environment variable |

### 5.4 Networks

Networks define the virtual switches your VMs connect to. Each network gets its
own IP subnet and optionally a DHCP server.

```yaml
    networks:
      my_network:                          # Your name for this network
        mode: nat                          # 'nat' (internet access) or 'route' (isolated)
        bridge:
          stp: 'on'                        # Spanning Tree Protocol
          delay: '0'                       # STP forwarding delay
        mac: '52:54:00:00:00:01'           # MAC address for the virtual bridge
        ip:
          address: '192.168.123.1'         # Gateway IP address
          netmask: '255.255.255.0'         # Subnet mask
          dhcp:                            # Optional — enable DHCP on this network
            range:
              start: '192.168.123.2'       # First assignable IP
              end: '192.168.123.254'       # Last assignable IP
        enable: True                       # Create and activate this network
        autostart: True                    # Start automatically on host boot
```

**Network modes explained:**

| Mode | Internet access | VM-to-VM | Host-to-VM | Use case |
|---|---|---|---|---|
| `nat` | ✅ Yes (via NAT) | ✅ Yes | ✅ Yes | General purpose — VMs can reach the internet |
| `route` | ❌ No | ✅ Yes | ❌ No | Isolated back-end networks (e.g., database replication) |

> 💡 **Choosing a subnet:** Pick a private range that doesn't conflict with your
> home/office network. Good defaults: `192.168.12x.0/24` or `10.0.x.0/24`.

### 5.5 VMs

Each VM is defined under the `vms` key:

```yaml
    vms:
      myvm01:                              # Unique VM identifier
        hostname: myvm01                   # Hostname (used in SSH config)
        cpus:                              # Optional CPU topology
          sockets: 1
          cores: 2
          threads: 1
        memory: 2048                       # RAM in MB
        disks:                             # Additional data disks (the OS disk is cloned from base_image)
          - name: disk01
            driver:
              name: qemu
              type: qcow2
            target: vdb                    # Device name inside the VM (/dev/vdb)
            size: 4096                     # Size in MiB
        network_adapters:                  # Connect to one or more networks
          - name: adapter_1
            link_state: 'up'              # 'up' or 'down'
            network_source: 'my_network'  # Must match a name from the networks section
```

**Key points about VMs:**

- The **OS disk** is automatically cloned from `base_image` — you don't define
  it here.
- **Additional disks** listed under `disks` are created empty (useful for data
  volumes).
- Each VM can have **multiple network adapters** on different networks.
- `target` is the Linux device name: `vdb`, `vdc`, `vdd`, etc. (`vda` is
  reserved for the OS disk).

### 5.6 Files

Boxman can write files into the cluster's `workdir` on your **host machine**
during provisioning:

```yaml
    files:
      hello.txt: |
        Hello from Boxman!
      setup.sh: |
        #!/bin/bash
        echo "Ready to go"
```

These files appear in the directory you set as `workdir`. This is useful for
generating Ansible inventories, SSH config wrappers, environment scripts, and
similar support files.

---

## 6. Build Your First Config from Scratch

Let's create a complete, working configuration file step by step.

### 6.1 Create a project directory

```bash
mkdir -p ~/workspaces/mylab
cd ~/workspaces/mylab
```

### 6.2 Store your base image password

```bash
echo -n 'your-root-password-here' > ~/workspaces/mylab/admin_pass.txt
chmod 600 ~/workspaces/mylab/admin_pass.txt
```

### 6.3 Create the config file

Create a file called `conf.yml` with the following content:

```yaml
---
version: 1.0
project: mylab
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
    workdir: ~/workspaces/mylab
    base_image: rocky9
    proxy_host: localhost
    admin_user: 'root'
    admin_pass: 'file://~/workspaces/mylab/admin_pass.txt'
    admin_key_name: id_ed25519_boxman
    ssh_config: ssh_config

    networks:
      lab_net:
        mode: nat
        bridge:
          stp: 'on'
          delay: '0'
        mac: '52:54:00:00:00:01'
        ip:
          address: '192.168.100.1'
          netmask: '255.255.255.0'
          dhcp:
            range:
              start: '192.168.100.2'
              end: '192.168.100.254'
        enable: True
        autostart: True

    vms:
      lab01:
        hostname: lab01
        cpus:
          sockets: 1
          cores: 1
          threads: 1
        memory: 2048
        disks:
          - name: data
            driver:
              name: qemu
              type: qcow2
            target: vdb
            size: 4096
        network_adapters:
          - name: adapter_1
            link_state: 'up'
            network_source: 'lab_net'

    files:
      readme.txt: |
        This cluster was provisioned by Boxman.
        SSH into lab01 with:
          ssh -F ssh_config lab01
```

### 6.4 Customise it for your system

Before provisioning, double-check these values:

| Setting | How to find the right value |
|---|---|
| `base_image` | Run `virsh -c qemu:///system list --all` — use the **Name** of your base VM |
| `admin_user` | The username you created during base image setup (e.g., `root`) |
| `admin_pass` | Must match the password you set in the base image |
| `virt_install_cmd` | Run `which virt-install` — prefix with `python` path if needed |
| `virt_clone_cmd` | Run `which virt-clone` — prefix with `python` path if needed |
| `virsh_cmd` | Run `which virsh` |
| Subnet (`192.168.100.x`) | Pick any unused private range — run `ip addr` to check for conflicts |

---

## 7. Provision Your Cluster

With your config file ready, run:

```bash
boxman --conf ~/workspaces/mylab/conf.yml provision
```

### What happens behind the scenes

Boxman performs these steps automatically:

1. **Renders** the config (Jinja2 → final YAML, saved as `conf.rendered.yml`)
2. **Registers** the project in its local cache (to prevent conflicts)
3. **Writes** any files from the `files` section to `workdir`
4. **Creates networks** (generates XML → `virsh net-define` →
   `virsh net-start` → configures iptables rules)
5. **Clones VMs** from the base image (`virt-clone`)
6. **Configures** CPU, memory, and disks (edits VM XML, creates disk images
   with `qemu-img`)
7. **Attaches** network interfaces
8. **Starts** VMs (`virsh start`)
9. **Waits** for VMs to get IP addresses (with automatic retries — up to 10
   minutes)
10. **Generates** an SSH keypair in `workdir`
11. **Copies** the public key to each VM (using `sshpass` + `ssh-copy-id`)
12. **Writes** an SSH config file for easy access

Provisioning typically takes **2–5 minutes** depending on disk speed and VM boot
time.

### Verify it worked

```bash
# Check the VM is running
virsh -c qemu:///system list

# Check generated files
ls ~/workspaces/mylab/
# You should see: conf.yml  ssh_config  id_ed25519_boxman  id_ed25519_boxman.pub  readme.txt
```

---

## 8. Connect to Your VM

Boxman generates an SSH config that makes connecting simple:

```bash
ssh -F ~/workspaces/mylab/ssh_config lab01
```

That's it. The SSH config maps the hostname `lab01` to the VM's IP address and
uses the auto-generated key.

---

## 9. Day-to-Day Commands

All commands follow the pattern:

```
boxman --conf <path/to/conf.yml> <command> [options]
```

### Cluster lifecycle

| Command | What it does |
|---|---|
| `boxman --conf conf.yml provision` | Create everything (networks, VMs, keys) |
| `boxman --conf conf.yml deprovision` | Tear down everything (VMs, networks, disks) |
| `boxman --conf conf.yml list` | List registered Boxman projects |

### VM power management

The `--machines` flag lets you target specific VMs (comma-separated) or `all`
(the default).

| Command | What it does |
|---|---|
| `boxman --conf conf.yml start` | Start all VMs |
| `boxman --conf conf.yml start --machines lab01` | Start only `lab01` |
| `boxman --conf conf.yml suspend pause` | Pause (freeze) all VMs |
| `boxman --conf conf.yml suspend resume` | Resume paused VMs |
| `boxman --conf conf.yml suspend save` | Save VM state to disk (like hibernate) |

### Snapshots

| Command | What it does |
|---|---|
| `boxman --conf conf.yml snapshot create -n before-update` | Create a snapshot named `before-update` |
| `boxman --conf conf.yml snapshot list` | List all snapshots |
| `boxman --conf conf.yml snapshot restore -n before-update` | Restore to a snapshot |
| `boxman --conf conf.yml snapshot delete -n before-update` | Delete a snapshot |
| `boxman --conf conf.yml snapshot create -n snap1 --machines lab01` | Snapshot a specific VM only |

### Export

| Command | What it does |
|---|---|
| `boxman --conf conf.yml export --machines lab01 --dest ~/exports/` | Export VM as OVF |

---

## 10. Going Further — Multi-VM Cluster

Here's an example with two VMs on two networks — a public NAT network and a
private isolated one:

```yaml
---
version: 1.0
project: webstack
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
    workdir: ~/workspaces/webstack
    base_image: rocky9
    proxy_host: localhost
    admin_user: 'root'
    admin_pass: '${env:BOXMAN_ADMIN_PASS}'
    admin_key_name: id_ed25519_boxman
    ssh_config: ssh_config

    networks:
      public:
        mode: nat
        bridge:
          stp: 'on'
          delay: '0'
        mac: '52:54:00:00:00:01'
        ip:
          address: '192.168.120.1'
          netmask: '255.255.255.0'
          dhcp:
            range:
              start: '192.168.120.2'
              end: '192.168.120.254'
        enable: True
        autostart: True

      backend:
        mode: route
        bridge:
          stp: 'on'
          delay: '0'
        mac: '52:54:00:00:00:02'
        ip:
          address: '10.0.10.1'
          netmask: '255.255.255.0'
          dhcp:
            range:
              start: '10.0.10.2'
              end: '10.0.10.254'
        enable: True
        autostart: True

    vms:
      web01:
        hostname: web01
        cpus:
          sockets: 1
          cores: 2
          threads: 1
        memory: 2048
        network_adapters:
          - name: adapter_1
            link_state: 'up'
            network_source: 'public'
          - name: adapter_2
            link_state: 'up'
            network_source: 'backend'

      db01:
        hostname: db01
        cpus:
          sockets: 1
          cores: 2
          threads: 1
        memory: 4096
        disks:
          - name: pgdata
            driver:
              name: qemu
              type: qcow2
            target: vdb
            size: 20480
        network_adapters:
          - name: adapter_1
            link_state: 'up'
            network_source: 'backend'

    files:
      env.sh: |
        export SSH_CONFIG=${HOME}/workspaces/webstack/ssh_config
      ansible.cfg: |
        [defaults]
        host_key_checking = False
        [ssh_connection]
        pipelining = True
        ssh_args = -o ControlMaster=auto -o ControlPersist=60s
```

In this setup:

- **web01** has two NICs — it can reach the internet (via `public`) and talk to
  the database (via `backend`).
- **db01** has only one NIC on the `backend` network — it is completely isolated
  from the internet.
- **db01** has a 20 GB data disk mounted at `/dev/vdb`.

---

## 11. Advanced Features

### 11.1 Jinja2 templating in configs

Your config file is rendered as a Jinja2 template before parsing. This is
powerful for creating many similar VMs without copy-paste.

**Generate VMs with a loop:**

```yaml
    vms:
      {% for i in range(1, 6) %}
      node{{ "%02d" % i }}:
        hostname: node{{ "%02d" % i }}
        memory: 2048
        network_adapters:
          - name: adapter_1
            link_state: 'up'
            network_source: 'my_network'
      {% endfor %}
```

This creates `node01` through `node05` — each with 2 GB RAM.

**Generate multiple disks:**

```yaml
        disks:
          {% for suffix in 'bcde' %}
          - name: disk_{{ suffix }}
            driver:
              name: qemu
              type: qcow2
            target: vd{{ suffix }}
            size: 4096
          {% endfor %}
```

This creates four data disks: `vdb`, `vdc`, `vdd`, `vde`.

**Use environment variables:**

Environment variables are available as `environ` in the template:

```yaml
    admin_pass: '{{ environ["BOXMAN_ADMIN_PASS"] }}'
    # Or use the built-in resolver syntax:
    admin_pass: '${env:BOXMAN_ADMIN_PASS}'
```

### 11.2 Connecting to pre-existing libvirt networks

If you already have a libvirt network (e.g., the built-in `default` network),
you can attach VMs to it without Boxman managing it:

```yaml
        network_adapters:
          - name: adapter_ext
            link_state: 'up'
            network_source: 'default'
            is_global: True            # Don't expand this name — use it as-is
```

### 11.3 Cross-cluster network references

Network names can reference networks from other clusters or projects:

| Format | Meaning |
|---|---|
| `my_net` | Network in the current cluster |
| `cluster_2::my_net` | Network in `cluster_2` of the current project |
| `other_project::cluster_2::my_net` | Network in a different project entirely |

### 11.4 Network adapter options

```yaml
        network_adapters:
          - name: adapter_1
            link_state: 'up'              # 'up' or 'down'
            network_source: 'my_net'
            mac: '52:54:00:aa:bb:cc'      # Optional: set a specific MAC address
            model: 'virtio'               # Optional: NIC model (default: virtio)
```

---

## 12. Verify with Ansible (Optional)

If you have Ansible installed, you can verify your VM with a simple playbook.

### Create `playbook.yml`

```yaml
- name: Verify Boxman VM
  hosts: all
  become: true
  gather_facts: false

  tasks:
    - name: Ping the host
      ping:

    - name: Install htop
      package:
        name: htop
        state: present

    - name: Write a test config line
      lineinfile:
        path: /etc/myapp.conf
        line: 'option=true'
        create: yes
```

### Create a minimal inventory

Create `inventory.ini`:

```ini
[default]
lab01 ansible_host=<VM_IP_ADDRESS> ansible_user=root ansible_ssh_private_key_file=~/workspaces/mylab/id_ed25519_boxman
```

> 💡 **Finding the IP:** After provisioning, check the generated `ssh_config`
> file — it contains the IP for each VM. Or run
> `virsh -c qemu:///system domifaddr <vm-name>`.

### Run the playbook

```bash
ansible-playbook -i inventory.ini playbook.yml \
  --ssh-extra-args='-o StrictHostKeyChecking=no'
```

If all tasks show **ok** or **changed**, your VM is fully functional.

---

## 13. Troubleshooting

### "Permission denied" or "Failed to connect to qemu:///system"

- Make sure your user is in the `libvirt` group: `groups $USER`
- Log out and back in after adding the group
- Try `virsh -c qemu:///system list` directly

### Provisioning hangs at "Waiting for IP address"

- The base image VM may not have DHCP client enabled
- Check that the network's DHCP range is set correctly
- Verify the base image boots successfully:
  `virsh -c qemu:///system start <base_image_name>` and check with
  `virt-manager`

### SSH key injection fails ("sshpass: command not found")

- Install `sshpass`:
  - Ubuntu/Debian: `sudo apt install sshpass`
  - CentOS/Rocky: `sudo dnf install sshpass`
  - Arch: `sudo pacman -S sshpass`

### "virt-install: command not found" or wrong path

- Run `which virt-install` and update `virt_install_cmd` in your config
- Same for `virt-clone` and `virsh`

### VMs won't start after host reboot

- Networks may need to be restarted. Run `boxman --conf conf.yml start`
- Or set `autostart: True` on your networks

### How to start fresh

```bash
boxman --conf conf.yml deprovision
```

This removes all VMs, networks, and disks created by the project. Then run
`provision` again.

---

## 14. Quick Reference

### Config file skeleton

```yaml
---
version: 1.0
project: <unique_project_name>
provider:
  libvirt:
    uri: qemu:///system
    use_sudo: True
    virt_install_cmd: '<path>'
    virt_clone_cmd: '<path>'
    virsh_cmd: '<path>'
  verbose: True

clusters:
  <cluster_name>:
    workdir: <path>                  # Where generated files go
    base_image: <libvirt_vm_name>    # VM to clone
    proxy_host: localhost
    admin_user: '<ssh_user>'
    admin_pass: '<password>'         # Plain | file://path | ${env:VAR}
    admin_key_name: id_ed25519_boxman
    ssh_config: ssh_config

    networks:
      <network_name>:
        mode: nat | route
        bridge: { stp: 'on', delay: '0' }
        mac: '<bridge_mac>'
        ip:
          address: '<gateway_ip>'
          netmask: '<mask>'
          dhcp:                      # Optional
            range: { start: '<ip>', end: '<ip>' }
        enable: True
        autostart: True

    vms:
      <vm_name>:
        hostname: <hostname>
        cpus: { sockets: 1, cores: 1, threads: 1 }   # Optional
        memory: <mb>                                   # Optional
        disks:                                         # Optional extra disks
          - name: <disk_name>
            driver: { name: qemu, type: qcow2 }
            target: vdb                                # vdb, vdc, vdd...
            size: <mib>
        network_adapters:
          - name: <adapter_name>
            link_state: 'up'
            network_source: '<network_name>'

    files:                           # Optional — files written to workdir
      <filename>: |
        <content>
```

### Command cheat sheet

```
boxman --conf conf.yml provision                             # Create everything
boxman --conf conf.yml deprovision                               # Tear down everything
boxman --conf conf.yml list                                  # List projects
boxman --conf conf.yml start [--machines vm1,vm2]            # Start VMs
boxman --conf conf.yml suspend pause [--machines ...]        # Pause VMs
boxman --conf conf.yml suspend resume [--machines ...]       # Resume VMs
boxman --conf conf.yml suspend save [--machines ...]         # Hibernate VMs
boxman --conf conf.yml snapshot create -n <name>             # Create snapshot
boxman --conf conf.yml snapshot list                         # List snapshots
boxman --conf conf.yml snapshot restore -n <name>            # Restore snapshot
boxman --conf conf.yml snapshot delete -n <name>             # Delete snapshot
boxman --conf conf.yml export --machines <vm> --dest <dir>   # Export as OVF
boxman --version                                             # Show version
```
