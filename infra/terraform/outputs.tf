output "cloud_ip" { value = aws_eip.cloud.public_ip }
output "public_url" { value = "http://${aws_eip.cloud.public_ip}/" }
output "ingest_url" { value = "https://${aws_eip.cloud.public_ip}:8443" }
output "s3_bucket" { value = aws_s3_bucket.media.bucket }
output "dynamodb_table" { value = aws_dynamodb_table.videos.name }
output "ssh" { value = "ssh -i ${var.ssh_private_key} ubuntu@${aws_eip.cloud.public_ip}" }
