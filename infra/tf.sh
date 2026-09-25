#!/usr/bin/env bash
# Run Terraform for one layer and one environment against the shared S3 state bucket.
#
#   infra/tf.sh <staging|prod> <layer> <init|plan|apply|destroy|output|...> [extra terraform args]
#   infra/tf.sh staging 01-network plan
#   infra/tf.sh prod 06-api apply -var image_tag=abc123
#
# State bucket: $TF_STATE_BUCKET, else `state_bucket` from infra/envs/<env>.tfvars.
# State key:    <env>/<layer>.tfstate  (S3 native locking: use_lockfile = true)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env="${1:?env (staging|prod)}"
layer="${2:?layer (e.g. 01-network)}"
cmd="${3:?terraform command}"
shift 3

vars="$here/envs/$env.tfvars"
[[ -f "$vars" ]] || { echo "no $vars" >&2; exit 2; }
[[ -d "$here/$layer" ]] || { echo "no layer $layer" >&2; exit 2; }

bucket="${TF_STATE_BUCKET:-$(sed -nE 's/^state_bucket[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$vars")}"
[[ -n "$bucket" && "$bucket" != CHANGE_ME* ]] || { echo "set state_bucket in $vars or TF_STATE_BUCKET" >&2; exit 2; }

cd "$here/$layer"
terraform init -input=false -reconfigure \
  -backend-config="bucket=$bucket" \
  -backend-config="key=$env/$layer.tfstate" >/dev/null

case "$cmd" in
  init) exit 0 ;;
  plan|apply|destroy|import|refresh|console)
    exec terraform "$cmd" -input=false -var-file="$vars" -var "state_bucket=$bucket" "$@" ;;
  *)
    exec terraform "$cmd" "$@" ;;
esac
