variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "ami_id" {
  type        = string
  description = "AMI built by aws/packer (see build/ami-manifest.json)."
}

variable "name" {
  type    = string
  default = "jarvis-agent"
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
  type        = string
  default     = "anthropic"
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
