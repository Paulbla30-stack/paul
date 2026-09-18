# OpenClaw agent-first AMI
# ========================
# Builds an Amazon Machine Image whose primary process is the OpenClaw
# agent.  Usage (from the repo root):
#
#   make ami-init                  # packer init (downloads the amazon plugin)
#   make ami-validate
#   make ami AWS_REGION=eu-west-2  # or: packer build aws/packer
#
# The build launches a temporary instance from the latest Amazon Linux
# 2023 AMI, uploads build/openclaw-src.tar.gz (git archive of HEAD), runs
# aws/scripts/provision.sh and aws/scripts/cleanup.sh, then snapshots it
# as "openclaw-agent-<base>-<arch>-<timestamp>".

packer {
  required_version = ">= 1.9.0"
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

# ---- Inputs ---------------------------------------------------------------

variable "region" {
  type        = string
  default     = "eu-west-2"
  description = "Region to build (and register the AMI) in."
}

variable "instance_type" {
  type        = string
  default     = "t3.small"
  description = "Build instance type. Use t4g.small with arch=arm64."
}

variable "arch" {
  type        = string
  default     = "x86_64"
  description = "AMI architecture: x86_64 or arm64."
  validation {
    condition     = contains(["x86_64", "arm64"], var.arch)
    error_message = "The arch must be x86_64 or arm64."
  }
}

variable "base" {
  type        = string
  default     = "al2023"
  description = "Base image family: al2023 (Amazon Linux 2023) or ubuntu (24.04)."
  validation {
    condition     = contains(["al2023", "ubuntu"], var.base)
    error_message = "The base must be al2023 or ubuntu."
  }
}

variable "ami_name_prefix" {
  type    = string
  default = "openclaw-agent"
}

variable "openclaw_version" {
  type    = string
  default = "1.0.0"
}

variable "volume_size_gb" {
  type    = number
  default = 8
}

variable "subnet_id" {
  type        = string
  default     = ""
  description = "Optional subnet for the build instance (default VPC when empty)."
}

variable "ami_users" {
  type        = list(string)
  default     = []
  description = "Optional AWS account IDs to share the finished AMI with."
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}

variable "ssh_interface" {
  type        = string
  default     = "session_manager"
  description = "How Packer reaches the build instance: session_manager (SSM over HTTPS, no open port 22; needs the session-manager-plugin locally) or public_ip (plain SSH)."
  validation {
    condition     = contains(["session_manager", "public_ip", "private_ip"], var.ssh_interface)
    error_message = "The ssh_interface must be session_manager, public_ip or private_ip."
  }
}

# ---- Derived values -------------------------------------------------------

locals {
  timestamp = formatdate("YYYYMMDD-hhmmss", timestamp())
  ami_name  = "${var.ami_name_prefix}-${var.base}-${var.arch}-${local.timestamp}"

  base_filters = {
    al2023 = {
      name     = "al2023-ami-2023.*-${var.arch}"
      owners   = ["amazon"]
      ssh_user = "ec2-user"
    }
    ubuntu = {
      name     = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${var.arch == "arm64" ? "arm64" : "amd64"}-server-*"
      owners   = ["099720109477"] # Canonical
      ssh_user = "ubuntu"
    }
  }
  base = local.base_filters[var.base]

  tags = merge({
    Name            = local.ami_name
    Project         = "openclaw"
    Role            = "agent-first-os"
    OpenClawVersion = var.openclaw_version
    OpenClawProfile = "cloud"
    BaseImage       = var.base
    Architecture    = var.arch
    BuiltBy         = "packer"
  }, var.extra_tags)
}

# ---- Source ---------------------------------------------------------------

source "amazon-ebs" "openclaw" {
  region          = var.region
  instance_type   = var.instance_type
  ami_name        = local.ami_name
  ami_description = "OpenClaw agent-first OS ${var.openclaw_version} (${var.base}, ${var.arch})"
  ami_users       = var.ami_users
  subnet_id       = var.subnet_id != "" ? var.subnet_id : null

  source_ami_filter {
    filters = {
      name                = local.base.name
      root-device-type    = "ebs"
      virtualization-type = "hvm"
      architecture        = var.arch
    }
    owners      = local.base.owners
    most_recent = true
  }

  ssh_username  = local.base.ssh_user
  ssh_interface = var.ssh_interface
  ssh_timeout   = "10m"

  # With session_manager the build instance needs an SSM-capable role;
  # Packer creates and removes a temporary one.
  temporary_iam_instance_profile_policy_document {
    Version = "2012-10-17"
    Statement {
      Effect = "Allow"
      Action = [
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
        "ec2messages:GetMessages",
        "ec2messages:AcknowledgeMessage",
        "ec2messages:SendReply",
      ]
      Resource = ["*"]
    }
  }

  # IMDSv2 only - the bootstrap service uses token-based calls.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }
  imds_support = "v2.0"

  launch_block_device_mappings {
    device_name           = var.base == "ubuntu" ? "/dev/sda1" : "/dev/xvda"
    volume_size           = var.volume_size_gb
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  ena_support   = true
  sriov_support = true

  tags          = local.tags
  snapshot_tags = local.tags
  run_tags      = merge(local.tags, { Name = "packer-build-${local.ami_name}" })
}

# ---- Build ----------------------------------------------------------------

build {
  name    = "openclaw-ami"
  sources = ["source.amazon-ebs.openclaw"]

  # Upload the repository as a tarball made by `make ami-bundle`
  # (git archive of HEAD, so the image matches a commit exactly).
  provisioner "file" {
    source      = "build/openclaw-src.tar.gz"
    destination = "/tmp/openclaw-src.tar.gz"
  }

  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; sudo env SRC=/tmp/openclaw-src OPENCLAW_VERSION=${var.openclaw_version} {{ .Path }}"
    script          = "aws/scripts/provision.sh"
  }

  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; sudo {{ .Path }}"
    script          = "aws/scripts/cleanup.sh"
  }

  post-processor "manifest" {
    output     = "build/ami-manifest.json"
    strip_path = true
    custom_data = {
      version = var.openclaw_version
      base    = var.base
      arch    = var.arch
    }
  }
}
