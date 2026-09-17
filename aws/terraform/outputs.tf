output "instance_id" {
  value = aws_instance.openclaw.id
}

output "private_ip" {
  value = aws_instance.openclaw.private_ip
}

output "public_ip" {
  value = aws_instance.openclaw.public_ip
}

output "ssm_session_command" {
  value = "aws ssm start-session --region ${var.region} --target ${aws_instance.openclaw.id}"
}

output "serial_console_command" {
  value = "aws ec2-instance-connect send-serial-console-ssh-public-key --region ${var.region} --instance-id ${aws_instance.openclaw.id} --ssh-public-key file://~/.ssh/id_ed25519.pub && ssh ${aws_instance.openclaw.id}.port0@serial-console.ec2-instance-connect.${var.region}.aws"
}
