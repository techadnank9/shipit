terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {
  region = "us-west-1"
}

resource "aws_s3_bucket" "receipts" {
  bucket = "refund-receipts"
}

resource "aws_s3_bucket_acl" "receipts" {
  bucket = aws_s3_bucket.receipts.id
  acl    = "public-read"
}
