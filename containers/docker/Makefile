# Load runtime-generated .env (written by boxman's DockerComposeRuntime)
# so that BOXMAN_INSTANCE_NAME, BOXMAN_DATA_DIR, etc. match the running container.
-include .env

export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

BOXMAN_DATA_DIR ?= ./data
export BOXMAN_DATA_DIR := $(shell realpath $(BOXMAN_DATA_DIR))

BOXMAN_INSTANCE_NAME ?= default
export BOXMAN_INSTANCE_NAME

BOXMAN_PROJECT_DIR ?= $(shell pwd)
export BOXMAN_PROJECT_DIR

SSH_LINK_DIR := $(HOME)/.ssh/config.d/boxman
SSH_LINK := $(SSH_LINK_DIR)/boxman-$(BOXMAN_INSTANCE_NAME).conf

up: ## Start the container and link ssh config
	docker compose up -d
	@$(MAKE) link-ssh

build: ## Build the docker image
	docker compose build

rebuild: ## Rebuild the docker image ignoring cache
	docker compose build --no-cache

down:
	docker compose down

restart:
	docker compose down
	docker compose up --build -d

logs:
	docker compose logs -f

ssh: ## SSH into the container (pass cmd="..." to run a command non-interactively)
ifdef cmd
	ssh -F $(SSH_LINK) -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null boxman-$(BOXMAN_INSTANCE_NAME) $(cmd)
else
	ssh -F $(SSH_LINK) boxman-$(BOXMAN_INSTANCE_NAME)
endif

ssh-debug: ## SSH into the container with verbose output
	ssh -vvv -F $(SSH_LINK) boxman-$(BOXMAN_INSTANCE_NAME)

status: ## Show container status, sshd status, and key permissions
	@echo "=== Container status ==="
	docker compose ps
	@echo ""
	@echo "=== Supervisor status ==="
	docker compose exec boxman-libvirt supervisorctl -c /etc/supervisord.conf status || true
	@echo ""
	@echo "=== SSHD logs ==="
	docker compose exec boxman-libvirt cat /var/log/supervisor/sshd.stderr.log 2>/dev/null || true
	@echo ""
	@echo "=== SSH key on host ==="
	ls -la $(BOXMAN_DATA_DIR)/ssh/ 2>/dev/null || echo "No SSH dir found"
	@echo ""
	@echo "=== SSH config ==="
	cat $(SSH_LINK) 2>/dev/null || echo "No SSH config found"
	@echo ""
	@echo "=== Port check ==="
	ss -tlnp | grep 2222 || echo "Port 2222 not listening"

virsh: ## List VMs via libvirt socket
	virsh -c "qemu+unix:///system?socket=$(BOXMAN_DATA_DIR)/libvirt-run/libvirt-sock" list --all

# Cleanup targets

clean-data: ## Remove all data (images, ssh keys, libvirt sockets) from the host data dir
	@echo "Removing data dir: $(BOXMAN_DATA_DIR)"
	sudo rm -rfv $(BOXMAN_DATA_DIR)

clean-volumes: ## Remove named docker compose volumes
	docker compose down -v

clean-images: ## Remove the built docker image
	docker compose down --rmi all

clean-containers: ## Remove stopped containers and orphans
	docker compose down

clean-all: ## Full cleanup: stop containers, remove volumes, images, and data dir
	docker compose down -v --rmi all
	sudo rm -rfv $(BOXMAN_DATA_DIR)
	@rm -f $(SSH_LINK)
	@echo "Full cleanup complete."

clean-dangling: ## Remove dangling docker images and volumes system-wide
	docker image prune -f
	docker volume prune -f

link-ssh: ## Symlink ssh config to ~/.ssh/boxman/boxman-<instance>.conf
	@mkdir -p $(SSH_LINK_DIR)
	@ln -sf $(BOXMAN_DATA_DIR)/ssh/boxman.conf $(SSH_LINK)
	@echo "Linked $(SSH_LINK) -> $(BOXMAN_DATA_DIR)/ssh/boxman.conf"

unlink-ssh: ## Remove the ssh config symlink
	@rm -f $(SSH_LINK)
	@echo "Removed $(SSH_LINK)"

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: up build rebuild down restart logs ssh ssh-debug status virsh \
        link-ssh unlink-ssh \
        clean-data clean-volumes clean-images clean-containers clean-all clean-dangling help
