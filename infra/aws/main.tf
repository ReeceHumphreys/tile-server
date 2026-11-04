terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "tile-service" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "bucket_name" {
  type        = string
  description = "Globally unique S3 bucket name."
}

variable "release_id" {
  type        = string
  description = "Immutable release uploaded with tile-service upload --release."
  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$", var.release_id))
    error_message = "Use a safe, unique release identifier."
  }
}

variable "allowed_origins" {
  type    = list(string)
  default = ["*"]
}

variable "price_class" {
  type        = string
  default     = "PriceClass_100"
  description = "CloudFront edge geography; select a broader class when needed."
}

resource "aws_s3_bucket" "tiles" {
  bucket        = var.bucket_name
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "tiles" {
  bucket                  = aws_s3_bucket.tiles.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_cors_configuration" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  cors_rule {
    allowed_headers = ["Range", "If-Match", "If-None-Match"]
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = var.allowed_origins
    expose_headers  = ["ETag", "Accept-Ranges", "Content-Length", "Content-Range"]
    max_age_seconds = 3600
  }
}

resource "aws_cloudfront_origin_access_control" "tiles" {
  name                              = "${var.bucket_name}-tiles"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "archives" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_response_headers_policy" "tiles" {
  name = "${var.bucket_name}-cors"
  cors_config {
    access_control_allow_credentials = false
    access_control_allow_headers { items = ["Range", "If-Match", "If-None-Match"] }
    access_control_allow_methods { items = ["GET", "HEAD", "OPTIONS"] }
    access_control_allow_origins { items = var.allowed_origins }
    access_control_expose_headers { items = ["ETag", "Accept-Ranges", "Content-Length", "Content-Range"] }
    access_control_max_age_sec = 3600
    origin_override            = true
  }
}

resource "aws_cloudfront_distribution" "tiles" {
  enabled             = true
  comment             = "Planetary PMTiles ${var.release_id}"
  default_root_object = "release.json"
  price_class         = var.price_class
  http_version        = "http2and3"
  is_ipv6_enabled     = true
  origin {
    domain_name              = aws_s3_bucket.tiles.bucket_regional_domain_name
    origin_id                = "tiles"
    origin_path              = "/releases/${var.release_id}"
    origin_access_control_id = aws_cloudfront_origin_access_control.tiles.id
  }
  default_cache_behavior {
    target_origin_id           = "tiles"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.tiles.id
    compress                   = false
  }
  ordered_cache_behavior {
    path_pattern               = "*.pmtiles"
    target_origin_id           = "tiles"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.archives.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.tiles.id
    compress                   = false
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}

data "aws_iam_policy_document" "cdn_read" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.tiles.arn}/releases/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.tiles.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "tiles" {
  bucket = aws_s3_bucket.tiles.id
  policy = data.aws_iam_policy_document.cdn_read.json
}

data "aws_iam_policy_document" "publisher" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.tiles.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${aws_s3_bucket.tiles.arn}/releases/*"]
  }
}

output "bucket" { value = aws_s3_bucket.tiles.id }
output "file_service_url" { value = "https://${aws_cloudfront_distribution.tiles.domain_name}" }
output "publisher_policy_json" { value = data.aws_iam_policy_document.publisher.json }
