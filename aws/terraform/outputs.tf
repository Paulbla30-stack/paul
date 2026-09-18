output "instance_id" {
  value = aws_instance.jarvis.id
}

output "private_ip" {
  value = aws_instance.jarvis.private_ip
}

output "public_ip" {
  value = aws_instance.jarvis.public_ip
}

output "ssm_session_command" {
  value = "aws ssm start-session --region ${var.region} --target ${aws_instance.jarvis.id}"
}

output "serial_console_command" {
  value = "aws ec2-instance-connect send-serial-console-ssh-public-key --region ${var.region} --instance-id ${aws_instance.jarvis.id} --ssh-public-key file://~/.ssh/id_ed25519.pub && ssh ${aws_instance.jarvis.id}.port0@serial-console.ec2-instance-connect.${var.region}.aws"
}

output "ui_url" {
  value = var.ui_cidr != "" ? "https://${aws_instance.jarvis.public_ip}:${var.ui_port}/ui" : "(closed: set -var ui_cidr=<your ip>/32)"
}

output "ledger_bucket" {
  value = var.ledger_anchor ? aws_s3_bucket.ledger[0].bucket : "(no anchor: -var ledger_anchor=false)"
}

output "ledger_audit_command" {
  value = var.ledger_anchor ? "python3 aws/scripts/ledger_audit.py --bucket ${aws_s3_bucket.ledger[0].bucket} --writer ${var.name} --region ${var.region} --pubkey <pinned hex> --pin build/ledger.pin" : "(no anchor)"
}
