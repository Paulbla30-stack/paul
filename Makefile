# OpenClaw Bootable ISO - Build System
# =====================================

SHELL := /bin/bash
.PHONY: all iso rootfs clean test scan qemu lint help \
        headless status ami-bundle ami-init ami-validate ami ami-fmt tf-init tf-validate

# Directories
BUILD_DIR    := build
ROOTFS_DIR   := $(BUILD_DIR)/rootfs
ISO_DIR      := $(BUILD_DIR)/iso
ISO_OUTPUT   := $(BUILD_DIR)/openclaw.iso
SCRIPTS_DIR  := scripts
CONFIG_DIR   := config
SRC_DIR      := openclaw

# Python
PYTHON       := python3
PIP          := pip3

# ISO tools
GRUB_MKRESCUE := grub-mkrescue
XORRISO       := xorriso

# AWS AMI (Packer)
PACKER        := packer
TERRAFORM     := terraform
PACKER_DIR    := aws/packer
AWS_REGION    ?= eu-west-2
AMI_ARCH      ?= x86_64
AMI_BASE      ?= al2023
AMI_INSTANCE  ?= t3.small
AMI_BUNDLE    := $(BUILD_DIR)/openclaw-src.tar.gz
PACKER_VARS   := -var region=$(AWS_REGION) -var arch=$(AMI_ARCH) \
                 -var base=$(AMI_BASE) -var instance_type=$(AMI_INSTANCE)

# Kernel (use host kernel for now, override for custom)
KERNEL       ?= /boot/vmlinuz-$(shell uname -r)
INITRD       ?= ""

# ---- Targets ----

help: ## Show this help
	@echo "OpenClaw Bootable ISO Build System"
	@echo "=================================="
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

all: iso ## Build everything (default)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# ---- Root Filesystem ----

rootfs: $(BUILD_DIR) ## Build the root filesystem
	@echo "[*] Building root filesystem..."
	mkdir -p $(ROOTFS_DIR)/{bin,sbin,etc,proc,sys,dev,tmp,var,usr/local/bin,usr/lib/openclaw,root}
	# Install OpenClaw agent
	cp -r $(SRC_DIR)/ $(ROOTFS_DIR)/usr/lib/openclaw/
	# Install configuration
	mkdir -p $(ROOTFS_DIR)/etc/openclaw
	cp rootfs/etc/openclaw/config.yaml $(ROOTFS_DIR)/etc/openclaw/
	# Install init scripts
	cp rootfs/etc/init.d/openclaw $(ROOTFS_DIR)/etc/init.d/ 2>/dev/null || true
	# Install launcher
	cp rootfs/usr/local/bin/openclaw $(ROOTFS_DIR)/usr/local/bin/
	chmod +x $(ROOTFS_DIR)/usr/local/bin/openclaw
	# Install boot init
	cp $(SCRIPTS_DIR)/init.sh $(ROOTFS_DIR)/init
	chmod +x $(ROOTFS_DIR)/init
	@echo "[+] Root filesystem built at $(ROOTFS_DIR)"

# ---- ISO Image ----

iso: rootfs ## Build the bootable ISO image
	@echo "[*] Building bootable ISO..."
	mkdir -p $(ISO_DIR)/boot/grub
	# Copy GRUB config
	cp $(CONFIG_DIR)/grub.cfg $(ISO_DIR)/boot/grub/grub.cfg
	# Copy kernel if available
	@if [ -f "$(KERNEL)" ]; then \
		cp $(KERNEL) $(ISO_DIR)/boot/vmlinuz; \
		echo "[+] Kernel copied"; \
	else \
		echo "[!] No kernel found at $(KERNEL), ISO will need kernel added manually"; \
	fi
	# Create initramfs from rootfs
	@echo "[*] Creating initramfs..."
	cd $(ROOTFS_DIR) && find . | cpio -o -H newc 2>/dev/null | gzip > $(CURDIR)/$(ISO_DIR)/boot/initramfs.gz
	@echo "[+] Initramfs created"
	# Build ISO with GRUB
	@if command -v $(GRUB_MKRESCUE) &>/dev/null; then \
		$(GRUB_MKRESCUE) -o $(ISO_OUTPUT) $(ISO_DIR) 2>/dev/null; \
		echo "[+] ISO created: $(ISO_OUTPUT)"; \
	else \
		echo "[!] grub-mkrescue not found. Creating raw ISO structure..."; \
		bash $(SCRIPTS_DIR)/build-iso.sh $(ISO_DIR) $(ISO_OUTPUT); \
	fi

# ---- Testing ----

test: ## Run test suite
	@echo "[*] Running OpenClaw tests..."
	$(PYTHON) -m pytest tests/ -v --tb=short 2>/dev/null || \
		$(PYTHON) -m unittest discover -s tests -v

scan: ## Run security vulnerability scan
	@echo "[*] Running security vulnerability scan..."
	$(PYTHON) -m openclaw.security.scanner --target . --report $(BUILD_DIR)/security-report.txt
	@echo "[+] Security report: $(BUILD_DIR)/security-report.txt"

# ---- Development ----

lint: ## Run linter on source code
	$(PYTHON) -m flake8 $(SRC_DIR)/ --max-line-length=100 || true
	$(PYTHON) -m mypy $(SRC_DIR)/ --ignore-missing-imports || true

qemu: iso ## Boot ISO in QEMU for testing
	@echo "[*] Launching QEMU..."
	qemu-system-x86_64 \
		-cdrom $(ISO_OUTPUT) \
		-m 2048 \
		-enable-kvm \
		-vga virtio \
		-usb -device usb-tablet \
		-boot d

# ---- Headless / cloud mode ----

headless: ## Run the agent headless locally for a few cycles (no hardware)
	PYTHONPATH=. $(PYTHON) -m openclaw.main --no-hardware --headless \
		--config rootfs/etc/openclaw/config-aws.yaml \
		--max-cycles $(or $(CYCLES),5) --cycle-interval 1 \
		--status-port 8471 --status-file $(BUILD_DIR)/status.json

status: ## Query a running headless agent
	PYTHONPATH=. $(PYTHON) -m openclaw.main --status \
		--config rootfs/etc/openclaw/config-aws.yaml \
		--status-file $(BUILD_DIR)/status.json

# ---- AWS AMI ----

ami-bundle: $(BUILD_DIR) ## Tar the committed tree for upload to the build instance
	@echo "[*] Bundling source (git archive HEAD)..."
	git archive --format=tar.gz -o $(AMI_BUNDLE) HEAD
	@echo "[+] Bundle: $(AMI_BUNDLE)"

ami-init: ## Install the Packer amazon plugin
	$(PACKER) init $(PACKER_DIR)

ami-fmt: ## Format the Packer template
	$(PACKER) fmt $(PACKER_DIR)

ami-validate: ami-bundle ## Validate the Packer template
	$(PACKER) validate $(PACKER_VARS) $(PACKER_DIR)

ami: ami-validate ## Build the OpenClaw agent-first AMI in $(AWS_REGION)
	@echo "[*] Building AMI ($(AMI_BASE), $(AMI_ARCH)) in $(AWS_REGION)..."
	$(PACKER) build $(PACKER_VARS) $(PACKER_DIR)
	@echo "[+] AMI manifest: $(BUILD_DIR)/ami-manifest.json"

tf-init: ## terraform init for the launch example
	$(TERRAFORM) -chdir=aws/terraform init

tf-validate: tf-init ## terraform validate for the launch example
	$(TERRAFORM) -chdir=aws/terraform validate

# ---- Cleanup ----

clean: ## Remove build artifacts
	rm -rf $(BUILD_DIR)
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "[+] Clean complete"
