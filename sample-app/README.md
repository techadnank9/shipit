# Sample app: Refunds desk

The demo "customer system". It has two planted problems:

1. **Double refund.** Retrying (or double-clicking) with the same `Idempotency-Key` creates a second refund.
2. **Public receipts bucket.** `terraform/main.tf` makes the S3 bucket public-read, which the policy gate blocks.
