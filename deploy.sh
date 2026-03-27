#!/usr/bin/env bash
set -euo pipefail

PROFILE="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-west-2}"
KEY_NAME="allegiance-arena-key"
KEY_FILE="$HOME/.ssh/${KEY_NAME}.pem"
SG_NAME="allegiance-arena-sg"
INSTANCE_TYPE="t3.small"
AMI_ID=""

AWS="aws --profile $PROFILE --region $REGION"

echo "============================================================"
echo "  Allegiance Arena — EC2 Deployment"
echo "  Profile: $PROFILE | Region: $REGION"
echo "============================================================"

echo "[1/7] Looking up latest Amazon Linux 2023 AMI..."
AMI_ID=$($AWS ssm get-parameters \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query 'Parameters[0].Value' --output text)
echo "  AMI: $AMI_ID"

echo "[2/7] Setting up key pair..."
if [ -f "$KEY_FILE" ]; then
    echo "  Key file already exists: $KEY_FILE"
else
    $AWS ec2 create-key-pair \
        --key-name "$KEY_NAME" \
        --query 'KeyMaterial' --output text > "$KEY_FILE"
    chmod 400 "$KEY_FILE"
    echo "  Created: $KEY_FILE"
fi

echo "[3/7] Setting up security group..."
SG_ID=$($AWS ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    SG_ID=$($AWS ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "Allegiance Arena - HTTP + SSH" \
        --query 'GroupId' --output text)
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" --protocol tcp --port 22 --cidr 0.0.0.0/0
    $AWS ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" --protocol tcp --port 80 --cidr 0.0.0.0/0
    echo "  Created SG: $SG_ID"
else
    echo "  Using existing SG: $SG_ID"
fi

echo "[4/7] Launching EC2 instance ($INSTANCE_TYPE)..."

USER_DATA=$(cat <<'USERDATA'
#!/bin/bash
set -e
yum update -y
yum install -y docker git
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

mkdir -p /usr/local/lib/docker/cli-plugins
curl -sL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

touch /tmp/docker-ready
USERDATA
)

INSTANCE_ID=$($AWS ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SG_ID" \
    --user-data "$USER_DATA" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=allegiance-arena}]" \
    --query 'Instances[0].InstanceId' --output text)
echo "  Instance: $INSTANCE_ID"

echo "[5/7] Waiting for instance to be running..."
$AWS ec2 wait instance-running --instance-ids "$INSTANCE_ID"
PUBLIC_IP=$($AWS ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "  Public IP: $PUBLIC_IP"

echo "[6/7] Waiting for instance initialization (this may take 1-2 minutes)..."
sleep 30

for i in {1..20}; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY_FILE" ec2-user@"$PUBLIC_IP" \
        "test -f /tmp/docker-ready" 2>/dev/null; then
        echo "  Docker is ready!"
        break
    fi
    echo "  Attempt $i/20 — waiting..."
    sleep 10
done

echo "[7/7] Deploying application..."

ssh -o StrictHostKeyChecking=no -i "$KEY_FILE" ec2-user@"$PUBLIC_IP" "mkdir -p ~/allegiance-arena/static"

scp -o StrictHostKeyChecking=no -i "$KEY_FILE" \
    Dockerfile docker-compose.yml nginx.conf requirements.txt \
    game_engine.py mcp_tools.py app.py .env \
    ec2-user@"$PUBLIC_IP":~/allegiance-arena/

scp -o StrictHostKeyChecking=no -i "$KEY_FILE" \
    static/dashboard.html \
    ec2-user@"$PUBLIC_IP":~/allegiance-arena/static/

ssh -o StrictHostKeyChecking=no -i "$KEY_FILE" ec2-user@"$PUBLIC_IP" \
    "cd ~/allegiance-arena && docker compose up -d --build"

echo ""
echo "============================================================"
echo "  DEPLOYMENT COMPLETE"
echo "============================================================"
echo "  Dashboard:  http://$PUBLIC_IP/"
echo "  MCP:        http://$PUBLIC_IP/mcp"
echo "  SSH:        ssh -i $KEY_FILE ec2-user@$PUBLIC_IP"
echo "  Instance:   $INSTANCE_ID"
echo "============================================================"

cat > .ec2-instance.json <<EOF
{
  "instance_id": "$INSTANCE_ID",
  "public_ip": "$PUBLIC_IP",
  "security_group_id": "$SG_ID",
  "key_name": "$KEY_NAME",
  "region": "$REGION"
}
EOF
echo "  Instance info saved to .ec2-instance.json"
