#!/usr/bin/env bash
set -euo pipefail

PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-west-2}"
AWS="aws --profile $PROFILE --region $REGION"

if [ ! -f .ec2-instance.json ]; then
    echo "No .ec2-instance.json found. Nothing to tear down."
    exit 1
fi

INSTANCE_ID=$(python3 -c "import json; print(json.load(open('.ec2-instance.json'))['instance_id'])")
SG_ID=$(python3 -c "import json; print(json.load(open('.ec2-instance.json'))['security_group_id'])")
KEY_NAME=$(python3 -c "import json; print(json.load(open('.ec2-instance.json'))['key_name'])")

echo "Terminating instance: $INSTANCE_ID"
$AWS ec2 terminate-instances --instance-ids "$INSTANCE_ID"

echo "Waiting for termination..."
$AWS ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"

echo "Deleting security group: $SG_ID"
$AWS ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null || echo "  (SG may already be deleted)"

echo "Deleting key pair: $KEY_NAME"
$AWS ec2 delete-key-pair --key-name "$KEY_NAME"
rm -f "$HOME/.ssh/${KEY_NAME}.pem"

rm -f .ec2-instance.json
echo "Teardown complete."
