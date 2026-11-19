# --- Stockage de l'énoncé : Amazon S3 (vidéos source + extraits) et DynamoDB (métadonnées) ---

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "media" {
  bucket        = "${var.name}-media-${random_id.suffix.hex}"
  force_destroy = true # « deploy.sh destroy » doit tout supprimer (ressources facturées)
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true # rien n'est public : la page passe par l'API publique en lecture seule
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_dynamodb_table" "videos" {
  name         = "${var.name}-videos-${random_id.suffix.hex}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "video_id"
  attribute {
    name = "video_id"
    type = "S"
  }
  server_side_encryption { enabled = true }
}

# --- Identité de la VM : rôle IAM limité à CE bucket et CETTE table (aucune clé sur la VM) ---
# AWS Academy / comptes restreints : la création de rôles est souvent interdite ; on réutilise alors
# le profil fourni (ex. TF_VAR_instance_profile_name=LabInstanceProfile).
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "access" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.videos.arn]
  }
}

resource "aws_iam_role" "cloud" {
  count              = var.instance_profile_name == "" ? 1 : 0
  name               = "${var.name}-cloud-${random_id.suffix.hex}"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "cloud" {
  count  = var.instance_profile_name == "" ? 1 : 0
  name   = "vidp-storage"
  role   = aws_iam_role.cloud[0].id
  policy = data.aws_iam_policy_document.access.json
}

resource "aws_iam_instance_profile" "cloud" {
  count = var.instance_profile_name == "" ? 1 : 0
  name  = "${var.name}-cloud-${random_id.suffix.hex}"
  role  = aws_iam_role.cloud[0].name
}

locals {
  instance_profile = var.instance_profile_name != "" ? var.instance_profile_name : aws_iam_instance_profile.cloud[0].name
}
