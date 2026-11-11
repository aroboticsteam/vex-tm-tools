#!/bin/sh
# Bring a fresh single-node Garage to a usable state: cluster layout, the app's
# S3 key, the bucket and its permissions + CORS. Safe to re-run.
set -eu
apk add --no-cache curl jq aws-cli >/dev/null
. /secrets/env
API=http://garage:3903
auth="Authorization: Bearer $(cat /secrets/garage_admin_token)"
api() { curl -sf -H "$auth" -H 'Content-Type: application/json' "$@"; }

until api "$API/v1/status" >/dev/null 2>&1; do echo "waiting for garage admin API"; sleep 2; done

node=$(api "$API/v1/status" | jq -r '.node')
version=$(api "$API/v1/layout" | jq -r '.version')
if [ "$version" = "0" ]; then
  echo "assigning cluster layout"
  api -X POST "$API/v1/layout" \
    -d "[{\"id\":\"$node\",\"zone\":\"dc1\",\"capacity\":1099511627776,\"tags\":[]}]" >/dev/null
  api -X POST "$API/v1/layout/apply" -d '{"version":1}' >/dev/null
fi

if ! api "$API/v1/key?id=$S3_ACCESS_KEY" >/dev/null 2>&1; then
  echo "importing S3 key"
  api -X POST "$API/v1/key/import" \
    -d "{\"name\":\"tmtools\",\"accessKeyId\":\"$S3_ACCESS_KEY\",\"secretAccessKey\":\"$S3_SECRET_KEY\"}" >/dev/null
fi

bucket_id=$(api "$API/v1/bucket?globalAlias=$S3_BUCKET" 2>/dev/null | jq -r '.id // empty' || true)
if [ -z "$bucket_id" ]; then
  echo "creating bucket $S3_BUCKET"
  bucket_id=$(api -X POST "$API/v1/bucket" -d "{\"globalAlias\":\"$S3_BUCKET\"}" | jq -r '.id')
fi
api -X POST "$API/v1/bucket/allow" \
  -d "{\"bucketId\":\"$bucket_id\",\"accessKeyId\":\"$S3_ACCESS_KEY\",\"permissions\":{\"read\":true,\"write\":true,\"owner\":true}}" >/dev/null

# Browsers fetch presigned URLs cross-origin (video, WebGL chroma preview, recorder uploads).
export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" AWS_DEFAULT_REGION=us-east-1
aws --endpoint-url http://garage:3900 s3api put-bucket-cors --bucket "$S3_BUCKET" --cors-configuration \
  '{"CORSRules":[{"AllowedOrigins":["*"],"AllowedMethods":["GET","PUT","POST","HEAD"],"AllowedHeaders":["*"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3600}]}'
echo "garage ready: bucket $S3_BUCKET"
