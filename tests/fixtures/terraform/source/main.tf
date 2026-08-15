variable "bucket_name" {
  type        = string
  description = "Where build artefacts land."
}

resource "aws_s3_bucket" "artifacts" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.artifacts.arn
}
