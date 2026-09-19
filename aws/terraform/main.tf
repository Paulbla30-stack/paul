# Launch a Jarvis agent-first instance from a built AMI.
# ---------------------------------------------------------
#   cd aws/terraform
#   terraform init
#   terraform apply -var ami_id=ami-0123456789abcdef0
#
# The instance gets an IAM role for SSM Session Manager (no inbound
# ports needed), IMDSv2 with tags exposed, and the example user data so
# the agent boots with goals.  Reach the agent with:
#   aws ssm start-session --target <instance-id>
#   jarvis --status

terraform {
  required_version = ">= 1.5.0"

  # State lives in S3 so any machine (or agent) can manage the instance:
  #   terraform init -backend-config="bucket=<your-bucket>" \
  #                  -backend-config="key=jarvis/terraform.tfstate" \
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
    Project = "jarvis"
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

resource "aws_iam_role" "jarvis" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.jarvis.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read access to the Anthropic API key. Store it once, never in Terraform
# state, in AWS Secrets Manager (default) and/or SSM Parameter Store:
#   aws secretsmanager create-secret --name jarvis/anthropic-api-key \
#       --secret-string "$ANTHROPIC_API_KEY"
#   aws ssm put-parameter --name /jarvis/anthropic-api-key \
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

data "aws_secretsmanager_secret" "tunnel_token" {
  count = var.tunnel_token_secret != "" ? 1 : 0
  name  = startswith(var.tunnel_token_secret, "arn:") ? null : var.tunnel_token_secret
  arn   = startswith(var.tunnel_token_secret, "arn:") ? var.tunnel_token_secret : null
}

locals {
  tunnel_enabled = var.tunnel_token_secret != "" || var.tunnel_token_ssm_parameter != ""
  tunnel_statements = concat(
    var.tunnel_token_secret != "" ? [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.tunnel_token[0].arn
    }] : [],
    var.tunnel_token_ssm_parameter != "" ? [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${trimprefix(var.tunnel_token_ssm_parameter, "/")}"
    }] : [],
  )

  # cloudflared dials these on 7844. Published at
  # https://developers.cloudflare.com/tunnel/configuration/#firewall-rules
  # as individual addresses inside two /24s; the /24s are what the rule needs.
  cloudflare_edge_cidrs = ["198.41.192.0/24", "198.41.200.0/24"]
}

data "aws_secretsmanager_secret" "notify_destination" {
  count = var.notify_destination_secret != "" ? 1 : 0
  name  = startswith(var.notify_destination_secret, "arn:") ? null : var.notify_destination_secret
  arn   = startswith(var.notify_destination_secret, "arn:") ? var.notify_destination_secret : null
}

locals {
  notify_enabled = var.notify_destination_secret != "" && var.notify_channel != "none"

  # Publishing a text with no TopicArn is an account-wide action, so it cannot
  # be narrowed by resource. It is narrowed instead by everything around it:
  # one destination the model cannot read or change, a severity floor, an
  # hourly cap, a gap and quiet hours, all in code below the planner.
  notify_statements = local.notify_enabled ? concat(
    [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.notify_destination[0].arn
    }],
    var.notify_channel == "sns_sms" ? [{
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = "*"
    }] : [],
    var.notify_channel == "sns_topic" ? [{
      Effect   = "Allow"
      Action   = ["sns:Publish"]
      Resource = "arn:aws:sns:${var.region}:${data.aws_caller_identity.current.account_id}:*"
    }] : [],
    var.notify_channel == "ses_email" ? [{
      Effect   = "Allow"
      Action   = ["ses:SendEmail"]
      Resource = "*"
    }] : [],
  ) : []
}

resource "aws_iam_role_policy" "notify" {
  count = length(local.notify_statements) > 0 ? 1 : 0
  name  = "jarvis-notify"
  role  = aws_iam_role.jarvis.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.notify_statements
  })
}

# What the agent costs and what the account is accumulating. Read-only, and
# every action here is a Describe or a List: there is no delete in this policy,
# so "tell me what is piling up" cannot become "tidy it away". Cost Explorer
# only answers in us-east-1 and charges a cent a call, which is why the agent
# asks it on a slow clock rather than every cycle.
resource "aws_iam_role_policy" "estate" {
  count = var.estate_reporting ? 1 : 0
  name  = "jarvis-estate"
  role  = aws_iam_role.jarvis.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ce:GetCostAndUsage",
        "ec2:DescribeImages",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVolumes",
        "s3:ListAllMyBuckets",
      ]
      Resource = "*"
    }]
  })
}

# Bedrock: let the instance role invoke catalog models, inference profiles
# and imported models. Scope var.bedrock_model_arns down once you know the
# exact model or imported-model ARN.
resource "aws_iam_role_policy" "bedrock" {
  count = var.llm_provider == "bedrock" ? 1 : 0
  name  = "jarvis-bedrock"
  role  = aws_iam_role.jarvis.id
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

# The Glass Ledger witness: an Object Lock bucket the instance can write to
# but never delete from or read back. The audit (aws/scripts/ledger_audit.py)
# runs elsewhere against this copy with a pinned public key.
resource "aws_s3_bucket" "ledger" {
  count               = var.ledger_anchor ? 1 : 0
  bucket              = "jarvis-ledger-${data.aws_caller_identity.current.account_id}-${var.name}"
  object_lock_enabled = true
  force_destroy       = false
  tags                = local.tags
}

resource "aws_s3_bucket_versioning" "ledger" {
  count  = var.ledger_anchor ? 1 : 0
  bucket = aws_s3_bucket.ledger[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "ledger" {
  count      = var.ledger_anchor ? 1 : 0
  bucket     = aws_s3_bucket.ledger[0].id
  depends_on = [aws_s3_bucket_versioning.ledger]
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.ledger_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "ledger" {
  count                   = var.ledger_anchor ? 1 : 0
  bucket                  = aws_s3_bucket.ledger[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ledger" {
  count  = var.ledger_anchor ? 1 : 0
  bucket = aws_s3_bucket.ledger[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_role_policy" "ledger_anchor" {
  count = var.ledger_anchor ? 1 : 0
  name  = "jarvis-ledger-anchor"
  role  = aws_iam_role.jarvis.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AppendOnly"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.ledger[0].arn}/ledger/*"
      },
      {
        Sid    = "NeverReadOrRelease"
        Effect = "Deny"
        Action = [
          "s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "s3:ListBucketVersions",
          "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObjectRetention",
          "s3:PutObjectLegalHold", "s3:BypassGovernanceRetention", "s3:PutBucketObjectLockConfiguration",
          "s3:DeleteBucket", "s3:PutLifecycleConfiguration",
        ]
        Resource = [aws_s3_bucket.ledger[0].arn, "${aws_s3_bucket.ledger[0].arn}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "llm_key" {
  count = length(local.llm_key_statements) > 0 && var.llm_provider == "anthropic" ? 1 : 0
  name  = "jarvis-llm-key"
  role  = aws_iam_role.jarvis.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.llm_key_statements
    # Secrets or parameters encrypted with a customer-managed KMS key also
    # need kms:Decrypt on that key.
  })
}

# The tunnel token is a credential, fetched at boot exactly like the LLM key
# and written 0600 to a file only the cloudflared user can read.
resource "aws_iam_role_policy" "tunnel_token" {
  count = length(local.tunnel_statements) > 0 ? 1 : 0
  name  = "jarvis-tunnel-token"
  role  = aws_iam_role.jarvis.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.tunnel_statements
  })
}

resource "aws_iam_instance_profile" "jarvis" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.jarvis.name
}

# ---- Network: outbound only unless ssh_cidr is set ------------------------

# Inbound rules are separate resources, NOT inline ingress blocks, and this is
# not a style preference. On aws_security_group the inline ingress attribute is
# Optional AND Computed: a dynamic block whose for_each goes to zero renders the
# attribute unset rather than empty, and an unset Computed attribute keeps
# whatever is already on the group. So setting ui_cidr = "" produced a plan that
# said "no changes" while the port stayed open to the whole internet. A control
# that silently does nothing is worse than no control, because you stop looking.
# Separate rule resources are deleted when their count goes to zero, so closing
# the port is a thing the plan actually shows you.
#
# The egress blocks below stay inline because they are never empty: lock_egress
# false yields one rule and true yields at least four, so the zero case that
# triggers the bug cannot arise. If that ever changes, move them out too.
resource "aws_security_group" "jarvis" {
  name_prefix = "${var.name}-"
  description = "Jarvis agent instance"
  vpc_id      = data.aws_vpc.selected.id
  tags        = local.tags

  # Outbound. The default used to be every protocol to the whole internet,
  # which meant a reverse shell, an scp or a DNS tunnel on any port were a
  # network away. Locked, the instance keeps exactly what it needs: HTTPS for
  # Bedrock, S3 and SSM, and DNS. That does not stop an HTTPS POST to an
  # arbitrary host, which is what the VPC endpoints below are for.
  dynamic "egress" {
    for_each = var.lock_egress ? [] : [1]
    content {
      description = "All outbound"
      from_port   = 0
      to_port     = 0
      protocol    = "-1"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  dynamic "egress" {
    for_each = var.lock_egress ? [1] : []
    content {
      description = "HTTPS to AWS APIs (Bedrock, S3, SSM)"
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  dynamic "egress" {
    for_each = var.lock_egress ? [1] : []
    content {
      description = "DNS to the VPC resolver"
      from_port   = 53
      to_port     = 53
      protocol    = "udp"
      cidr_blocks = [data.aws_vpc.selected.cidr_block]
    }
  }

  # The tunnel dials out on 7844 and holds the connection open, which is what
  # lets the UI have no inbound rule at all. Narrow to Cloudflare's published
  # edge ranges rather than the whole internet.
  dynamic "egress" {
    for_each = var.lock_egress && local.tunnel_enabled ? [1] : []
    content {
      description = "Cloudflare Tunnel edge (http2)"
      from_port   = 7844
      to_port     = 7844
      protocol    = "tcp"
      cidr_blocks = local.cloudflare_edge_cidrs
    }
  }

  dynamic "egress" {
    for_each = var.lock_egress && local.tunnel_enabled ? [1] : []
    content {
      description = "Cloudflare Tunnel edge (quic)"
      from_port   = 7844
      to_port     = 7844
      protocol    = "udp"
      cidr_blocks = local.cloudflare_edge_cidrs
    }
  }

  dynamic "egress" {
    for_each = var.lock_egress ? [1] : []
    content {
      description = "NTP to the Amazon Time Sync service"
      from_port   = 123
      to_port     = 123
      protocol    = "udp"
      cidr_blocks = ["169.254.169.123/32"]
    }
  }

  dynamic "egress" {
    for_each = var.lock_egress ? [1] : []
    content {
      description = "DNS over TCP to the VPC resolver"
      from_port   = 53
      to_port     = 53
      protocol    = "tcp"
      cidr_blocks = [data.aws_vpc.selected.cidr_block]
    }
  }
}

# ---- Instance ---------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  count             = var.ssh_cidr != "" ? 1 : 0
  security_group_id = aws_security_group.jarvis.id
  description       = "SSH"
  cidr_ipv4         = var.ssh_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  tags              = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "ui" {
  count             = var.ui_cidr != "" ? 1 : 0
  security_group_id = aws_security_group.jarvis.id
  description       = "Jarvis web UI (HTTPS, token login)"
  cidr_ipv4         = var.ui_cidr
  from_port         = var.ui_port
  to_port           = var.ui_port
  ip_protocol       = "tcp"
  tags              = local.tags
}

resource "aws_instance" "jarvis" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.jarvis.id]
  iam_instance_profile        = aws_iam_instance_profile.jarvis.name
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
      Name          = var.name
      "jarvis:name" = var.name
    },
    var.agent_goal != "" ? { "jarvis:goal" = var.agent_goal } : {},
    var.llm_provider == "anthropic" && var.anthropic_api_key_secret != "" ? { "jarvis:llm-key-secret" = var.anthropic_api_key_secret } : {},
    var.llm_provider == "anthropic" && var.anthropic_api_key_ssm_parameter != "" ? { "jarvis:llm-key-parameter" = var.anthropic_api_key_ssm_parameter } : {},
    var.ledger_anchor ? { "jarvis:ledger-bucket" = aws_s3_bucket.ledger[0].bucket } : {},
    var.tunnel_token_secret != "" ? { "jarvis:tunnel-token-secret" = var.tunnel_token_secret } : {},
    var.tunnel_token_ssm_parameter != "" ? { "jarvis:tunnel-token-parameter" = var.tunnel_token_ssm_parameter } : {},
    local.notify_enabled ? { "jarvis:notify-destination-secret" = var.notify_destination_secret } : {},
  )
}

# An auto-assigned public IP changes whenever the instance is stopped and
# started. That moves the UI's origin, which loses the browser's stored login
# along with the bookmark. An Elastic IP attached to a running instance costs
# nothing and fixes it. Off by default because turning it on changes the
# address once, the next time you apply.
resource "aws_eip" "jarvis" {
  count    = var.static_ip ? 1 : 0
  instance = aws_instance.jarvis.id
  domain   = "vpc"
  tags     = { Name = var.name }
}
