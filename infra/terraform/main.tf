terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    tls    = { source = "hashicorp/tls", version = "~> 4.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    local  = { source = "hashicorp/local", version = "~> 2.5" }
  }
}

# Les clés AWS fournies par l'enseignant sont lues dans l'environnement
# (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN) : rien n'est écrit dans le code.
provider "aws" {
  region = var.region
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-${var.arch == "arm64" ? "arm64" : "amd64"}-server-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "deploy" {
  key_name   = "${var.name}-deploy"
  public_key = file(pathexpand(var.ssh_public_key))
}

resource "aws_security_group" "cloud" {
  name        = "${var.name}-cloud"
  description = "VidP-Edge cloud : moindre privilege"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH administration"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  ingress {
    description = "Page web publique (lecture seule)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "Ingestion TLS depuis les noeuds Edge"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = var.edge_cidrs
  }
  ingress {
    description = "Prometheus Cloud lu par Grafana de l'Edge"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = var.edge_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_eip" "cloud" {
  domain = "vpc"
  tags   = { Name = "${var.name}-cloud" }
}

# --- Identité du Cloud : certificat auto-signé lié à l'IP élastique (épinglé par l'Edge) ---
resource "tls_private_key" "ca" {
  algorithm = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  validity_period_hours = 24 * 30
  is_ca_certificate     = true # auto-signé : sert aussi d'ancre de confiance épinglée côté Edge
  allowed_uses          = ["cert_signing", "digital_signature", "key_encipherment", "server_auth"]
  ip_addresses          = [aws_eip.cloud.public_ip]
  subject { common_name = "vidp-cloud" }
}

# --- Secrets générés (jamais dans une image ni dans Git) ---
resource "random_password" "master" {
  length  = 40
  special = false
}

resource "random_password" "grafana" {
  length  = 20
  special = false
}

resource "aws_instance" "cloud" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.deploy.key_name
  vpc_security_group_ids = [aws_security_group.cloud.id]
  iam_instance_profile   = local.instance_profile

  metadata_options {
    http_tokens                 = "required" # IMDSv2 uniquement
    http_put_response_hop_limit = 2          # les conteneurs Docker doivent joindre IMDS pour le rôle IAM
  }
  root_block_device {
    volume_size = var.disk_gb
    volume_type = "gp3"
    encrypted   = true
  }
  tags = { Name = "${var.name}-cloud" }
}

resource "aws_eip_association" "cloud" {
  instance_id   = aws_instance.cloud.id
  allocation_id = aws_eip.cloud.id
}

# --- Fichiers consommés par Ansible (ignorés par Git, droits 0600) ---
resource "local_sensitive_file" "secrets" {
  filename        = "${path.module}/../ansible/inventory/secrets.generated.yml"
  file_permission = "0600"
  content = yamlencode({
    cloud_ip            = aws_eip.cloud.public_ip
    cloud_master_secret = random_password.master.result
    grafana_password    = random_password.grafana.result
    tls_cert_pem        = tls_self_signed_cert.ca.cert_pem
    tls_key_pem         = tls_private_key.ca.private_key_pem
    storage             = "aws"
    s3_bucket           = aws_s3_bucket.media.bucket
    ddb_table           = aws_dynamodb_table.videos.name
    aws_region          = var.region
  })
}

resource "local_file" "inventory" {
  filename = "${path.module}/../ansible/inventory/cloud.generated.ini"
  content  = <<-EOT
    [cloud]
    cloud01 ansible_host=${aws_eip.cloud.public_ip} ansible_user=ubuntu ansible_ssh_private_key_file=${pathexpand(var.ssh_private_key)}
  EOT
  depends_on = [aws_eip_association.cloud]
}
