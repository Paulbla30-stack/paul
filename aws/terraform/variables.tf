variable "region" {
  type = string
  # us-west-2, because that is where the estate IS. The default read eu-west-2
  # while every resource in the state file sits in us-west-2 -- the instance is
  # i-016f9f37fe6ca2ba8 in us-west-2b -- so every apply so far has depended on
  # somebody remembering `-var region=us-west-2` on the command line.
  #
  # Forgetting it does not fail. The provider points at London, the data
  # sources read London's default VPC, and Terraform finds none of the estate
  # it is holding state for. A default that is wrong and silent is worse than
  # no default.
  default = "us-west-2"
}

variable "ami_id" {
  type        = string
  description = "AMI built by aws/packer (see build/ami-manifest.json)."
}

variable "name" {
  type = string
  # jarvis, because that is the name the estate was built with. Almost every
  # resource here is named from this: the IAM role, the security group, the
  # instance, and -- the one that matters -- the ledger's witness bucket,
  # jarvis-ledger-485964361844-jarvis.
  #
  # With the default reading "jarvis-agent", a plan came back
  # "21 to add, 0 to change, 16 to destroy": the instance replaced, the role
  # replaced, and the Object-Locked ledger bucket destroyed and recreated
  # under a new name. Not drift. Terraform correctly comparing the estate
  # against a different estate that happens to share a state file.
  #
  # As with the region, the apply that built this passed the value on the
  # command line and the default has been a loaded gun ever since.
  default = "jarvis"
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "root_volume_gb" {
  type    = number
  default = 8
}

variable "vpc_id" {
  type        = string
  default     = ""
  description = "VPC to launch into; empty = the default VPC."
}

variable "subnet_id" {
  type        = string
  default     = ""
  description = "Subnet to launch into; empty = first subnet of the VPC."
}

variable "key_name" {
  type        = string
  default     = ""
  description = "Optional EC2 key pair for SSH (SSM works without one)."
}

variable "ssh_cidr" {
  type        = string
  default     = ""
  description = "Optional CIDR allowed to SSH in, e.g. 203.0.113.4/32. Empty = no inbound."
}

variable "user_data_file" {
  type        = string
  default     = ""
  description = "cloud-config file with a jarvis: block; empty = the bundled example."
}

variable "agent_goal" {
  type        = string
  default     = ""
  description = "Optional goal passed via the jarvis:goal instance tag."
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "anthropic_api_key_secret" {
  type        = string
  default     = "jarvis/anthropic-api-key"
  description = "AWS Secrets Manager secret (name or ARN) holding the Anthropic API key for the LLM planner. The value may be the bare key or JSON with an ANTHROPIC_API_KEY field. Empty disables the grant."
}

variable "anthropic_api_key_ssm_parameter" {
  type        = string
  default     = ""
  description = "Alternative: SSM SecureString holding the Anthropic API key. Empty disables the grant. With neither source set the agent runs on the rule planner."
}

variable "llm_provider" {
  type = string
  # bedrock, because that is what the box runs: the journal says
  # `provider=bedrock model=qwen.qwen3-235b-a22b-2507-v1:0` and the instance
  # role's bedrock policy is in the state file. There is no Anthropic key
  # anywhere and Paul's decision, 23 September 2026, is that there will not be
  # one -- the models come through the instance role, where the vigil gates
  # them and the ledger records them.
  #
  # The old default did more than mislabel. It made the llm_key data source
  # count = 1, which looked up a Secrets Manager secret named
  # jarvis/anthropic-api-key that does not exist, and that lookup failed
  # BEFORE any other work -- so `terraform import`, `plan` and `apply` all
  # errored on a resource nobody wanted. An unused variable took the whole
  # workspace down.
  default     = "bedrock"
  description = "Which planner the instance role is set up for: anthropic (Claude API key from Secrets Manager / SSM) or bedrock (Amazon Bedrock, no key). Must match llm.provider in the user data."
  validation {
    condition     = contains(["anthropic", "bedrock"], var.llm_provider)
    error_message = "llm_provider must be anthropic or bedrock."
  }
}

variable "bedrock_model_arns" {
  type        = list(string)
  default     = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*", "arn:aws:bedrock:*:*:imported-model/*"]
  description = "Resources the instance may invoke on Bedrock when llm_provider is bedrock. Narrow to your model or imported-model ARN in production."
}

variable "ui_cidr" {
  type        = string
  default     = ""
  description = "CIDR allowed to reach the web UI (chat, agent panel, uploads) over HTTPS, e.g. 203.0.113.4/32. Empty = closed."
}

variable "ui_port" {
  type    = number
  default = 8443
}

variable "ledger_anchor" {
  type        = bool
  default     = true
  description = "Create an S3 bucket with Object Lock as the off-box witness for the agent's Glass Ledger. The instance may only add objects; every version is retained for ledger_retention_days."
}

variable "ledger_retention_days" {
  type        = number
  default     = 30
  description = "Object Lock retention (COMPLIANCE mode) for ledger checkpoints and copies. Nobody, including the account root, can delete a version before it expires; the bucket cannot be emptied until then."
}

variable "static_ip" {
  description = "Attach an Elastic IP so the UI address survives a stop/start. Turning this on changes the public address once."
  type        = bool
  default     = false
}

variable "lock_egress" {
  description = "Restrict outbound to HTTPS and DNS instead of everything. Kills reverse shells, scp and non-443 exfiltration; does not stop an HTTPS POST to an arbitrary host."
  type        = bool
  default     = true
}

variable "tunnel_token_secret" {
  type        = string
  default     = ""
  description = "AWS Secrets Manager secret (name or ARN) holding the Cloudflare Tunnel token, from `cloudflared tunnel token <name>` or the Zero Trust dashboard. Setting it turns the tunnel on: the instance dials out to Cloudflare and the UI needs no inbound rule. Empty disables it."
}

variable "tunnel_token_ssm_parameter" {
  type        = string
  default     = ""
  description = "Alternative: SSM SecureString holding the Cloudflare Tunnel token. Empty disables it."
}

variable "tunnel_hostname" {
  type        = string
  default     = ""
  description = "Informational: the hostname the tunnel publishes, e.g. jarvis.example.co.uk. The mapping itself lives in Cloudflare, not here, because a token-run tunnel is configured remotely."
}

variable "notify_destination_secret" {
  type        = string
  default     = ""
  description = "AWS Secrets Manager secret (name or ARN) holding the operator's phone number in E.164 (+447700900123) or email address. Setting it lets the agent reach you when nobody is looking at the UI. Empty means the agent can only speak when the UI is open."
}

variable "notify_channel" {
  type        = string
  default     = "sns_sms"
  description = "How the agent reaches the operator: sns_sms (a text), sns_topic (fan out to an existing topic), or ses_email."
  validation {
    condition     = contains(["sns_sms", "sns_topic", "ses_email", "none"], var.notify_channel)
    error_message = "notify_channel must be sns_sms, sns_topic, ses_email or none."
  }
}

variable "estate_reporting" {
  description = "Let the agent read what the account spends (Cost Explorer) and what it is accumulating (old images, snapshots, buckets). Read-only: the policy contains no delete. Cost Explorer charges roughly a cent per query."
  type        = bool
  default     = true
}

variable "document_ocr" {
  description = <<-EOT
    Let the agent read scanned PDFs and photographs of documents with AWS
    Textract. Text PDFs and Office files are read on the box for nothing and
    need this for nothing; a scan has no text layer and cannot be read at all
    without it. Billed per page detected, and the page image leaves the box for
    Textract in this account and region. Off means a scan is reported as
    unread rather than silently blank.
  EOT
  type        = bool
  default     = true
}

variable "memory_backup" {
  description = <<-EOT
    Copy the agent's durable memory to S3 on a slow clock. It holds the
    operator profile, the goals given at runtime and everything the agent has
    worked out, on one EBS volume and nowhere else without this. Unlike the
    ledger's witness bucket this one is versioned rather than Object Locked,
    and readable back by the instance, because a person must be able to erase
    their own data and because restoring is the point.
  EOT
  type        = bool
  default     = true
}

variable "memory_backup_keep_days" {
  description = "How long superseded copies of the memory are kept before the lifecycle rule expires them."
  type        = number
  default     = 90
}

# The scout's DynamoDB table, for the Marketing tab's read-only view. Empty
# disables the grant entirely, which is the right default for an instance
# that has nothing to do with the scout.
variable "scout_table" {
  description = "DynamoDB table the agent may READ the scout's proposals from"
  type        = string
  default     = "jarvis-scout"
}
