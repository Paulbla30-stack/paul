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

  # State lives in S3 so any machine (or agent) can manage the instance:
  #   terraform init -backend-config="bucket=<your-bucket>" \
  #                  -backend-config="key=openclaw/terraform.tfstate" \
  #                  -backend-config="region=<region>"
  backend "s3" {}

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

# Read access to the Anthropic API key. Store it once, never in Terraform
# state, in AWS Secrets Manager (default) and/or SSM Parameter Store:
#   aws secretsmanager create-secret --name openclaw/anthropic-api-key \
#       --secret-string "$ANTHROPIC_API_KEY"
#   aws ssm put-parameter --name /openclaw/anthropic-api-key \
#       --type SecureString --value "$ANTHROPIC_API_KEY"
data "aws_caller_identity" "current" {}

data "aws_secretsmanager_secret" "llm_key" {
  count = var.llm_provider == "anthropic" && var.anthropic_api_key_secret != "" ? 1 : 0
  name  = startswith(var.anthropic_api_key_secret, "arn:") ? null : var.anthropic_api_key_secret
  arn   = startswith(var.anthropic_api_key_secret, "arn:") ? var.anthropic_api_key_secret : null
}

locals {
  llm_key_statements = var.llm_provider != "anthropic" ? [] : concat(
    var.anthropic_api_key_secret != "" ? [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.llm_key[0].arn
    }] : [],
    var.anthropic_api_key_ssm_parameter != "" ? [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.anthropic_api_key_ssm_parameter, "/")}"
    }] : [],
  )
}

# Bedrock: let the instance role invoke catalog models, inference profiles
# and imported models. Scope var.bedrock_model_arns down once you know the
# exact model or imported-model ARN.
resource "aws_iam_role_policy" "bedrock" {
  count = var.llm_provider == "bedrock" ? 1 : 0
  name  = "openclaw-bedrock"
  role  = aws_iam_role.openclaw.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
      ]
      Resource = var.bedrock_model_arns
    }]
  })
}

resource "aws_iam_role_policy" "llm_key" {
  count = length(local.llm_key_statements) > 0 && var.llm_provider == "anthropic" ? 1 : 0
  name  = "openclaw-llm-key"
  role  = aws_iam_role.openclaw.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.llm_key_statements
    # Secrets or parameters encrypted with a customer-managed KMS key also
    # need kms:Decrypt on that key.
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

  dynamic "ingress" {
    for_each = var.ui_cidr != "" ? [var.ui_cidr] : []
    content {
      description = "OpenClaw web UI (HTTPS, token login)"
      from_port   = var.ui_port
      to_port     = var.ui_port
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
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.openclaw.id]
  iam_instance_profile        = aws_iam_instance_profile.openclaw.name
  key_name                    = var.key_name != "" ? var.key_name : null
  user_data                   = local.user_data
  user_data_replace_on_change = true
  monitoring                  = true

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
    var.llm_provider == "anthropic" && var.anthropic_api_key_secret != "" ? { "openclaw:llm-key-secret" = var.anthropic_api_key_secret } : {},
    var.llm_provider == "anthropic" && var.anthropic_api_key_ssm_parameter != "" ? { "openclaw:llm-key-parameter" = var.anthropic_api_key_ssm_parameter } : {},
  )
}
