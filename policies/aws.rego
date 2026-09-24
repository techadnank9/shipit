# Infrastructure rules checked before any change is allowed to ship.
# Input: {"resources": [{"type", "name", "file", "attrs"}]} from the Carbon Copy reader.
package carboncopy.aws

import rego.v1

public_acls := {"public-read", "public-read-write", "authenticated-read"}

deny contains msg if {
	some r in input.resources
	r.type == "aws_s3_bucket_acl"
	r.attrs.acl in public_acls
	msg := sprintf("%s: S3 bucket ACL '%s' makes data public (aws_s3_bucket_acl.%s)", [r.file, r.attrs.acl, r.name])
}

deny contains msg if {
	some r in input.resources
	r.type == "aws_s3_bucket"
	r.attrs.acl in public_acls
	msg := sprintf("%s: S3 bucket %s is public", [r.file, r.name])
}

deny contains msg if {
	some r in input.resources
	r.type == "aws_security_group_rule"
	r.attrs.type == "ingress"
	"0.0.0.0/0" in r.attrs.cidr_blocks
	r.attrs.from_port <= 22
	r.attrs.to_port >= 22
	msg := sprintf("%s: SSH open to the internet (%s)", [r.file, r.name])
}

deny contains msg if {
	some r in input.resources
	r.type == "aws_db_instance"
	not r.attrs.storage_encrypted == true
	msg := sprintf("%s: database %s is not encrypted", [r.file, r.name])
}

deny contains msg if {
	some r in input.resources
	startswith(r.type, "aws_")
	not startswith(r.type, "aws_s3_bucket_")
	region := object.get(r.attrs, "region", "us-west-1")
	not region in {"us-west-1", "us-west-2"}
	msg := sprintf("%s: %s.%s is outside the allowed US West regions", [r.file, r.type, r.name])
}
