# Launch an OpenClaw agent-first instance from a built AMI.
# ---------------------------------------------------------
#   cd aws/terraform
#   terraform init
#   terraform apply -var ami_id=ami-0123456789abcdef0
#
# The instance gets an IAM role for SSM Session Manager (no inbound
# ports needed), IMDSv2 with tags exposed, and the example user data so
# the agent boots with goals.  Reach the agent with:
#   aws ssm start-session --target <instance-id>
#   openclaw --status

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_vpc" "selected" {
  default = var.vpc_id == ""
  id      = var.vpc_id == "" ? null : var.vpc_id
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.selected.id]
  }
}

locals {
  subnet_id = var.subnet_id != "" ? var.subnet_id : sort(data.aws_subnets.selected.ids)[0]
  user_data = var.user_data_file != "" ? file(var.user_data_file) : file("${path.module}/../cloud-init/user-data.example.yaml")
  tags = merge({
    Project = "openclaw"
    Role    = "agent-first-os"
  }, var.tags)
}

# ---- IAM: SSM access so no SSH port is required --------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "openclaw" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.openclaw.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read access to the Anthropic API key stored as an SSM SecureString.
# Put it there once (never in Terraform state):
#   aws ssm put-parameter --name /openclaw/anthropic-api-key \
#       --type SecureString --value "$ANTHROPIC_API_KEY"
data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "llm_key" {
  count = var.anthropic_api_key_ssm_parameter != "" ? 1 : 0
  name  = "openclaw-llm-key"
  role  = aws_iam_role.openclaw.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.anthropic_api_key_ssm_parameter}"
    }]
    # A SecureString under the default aws/ssm key needs nothing more; a
    # customer-managed KMS key also needs kms:Decrypt on that key.
  })
}

resource "aws_iam_instance_profile" "openclaw" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.openclaw.name
}

# ---- Network: outbound only unless ssh_cidr is set ------------------------

resource "aws_security_group" "openclaw" {
  name_prefix = "${var.name}-"
  description = "OpenClaw agent instance"
  vpc_id      = data.aws_vpc.selected.id
  tags        = local.tags

  dynamic "ingress" {
    for_each = var.ssh_cidr != "" ? [var.ssh_cidr] : []
    content {
      description = "SSH"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---- Instance ---------------------------------------------------------------

resource "aws_instance" "openclaw" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.openclaw.id]
  iam_instance_profile   = aws_iam_instance_profile.openclaw.name
  key_name               = var.key_name != "" ? var.key_name : null
  user_data              = local.user_data
  user_data_replace_on_change = true
  monitoring             = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  tags = merge(
    local.tags,
    {
      Name            = var.name
      "openclaw:name" = var.name
    },
    var.agent_goal != "" ? { "openclaw:goal" = var.agent_goal } : {},
    var.anthropic_api_key_ssm_parameter != "" ? { "openclaw:llm-key-parameter" = var.anthropic_api_key_ssm_parameter } : {},
  )
}
