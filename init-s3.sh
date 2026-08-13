#!/bin/bash
awslocal s3 mb s3://my-bucket
echo "==> Bucket 'my-bucket' created successfully in LocalStack!"

# A presigned POST goes from the browser straight to this bucket, which is a
# different origin than the API. Without a CORS rule the browser blocks the
# request before it is ever sent -- and the failure surfaces as an opaque
# network error, not an S3 error, which is a miserable thing to debug.
#
# AllowedOrigin '*' is a dev convenience. Narrow it to your frontend origin
# before this runs anywhere real.
awslocal s3api put-bucket-cors --bucket my-bucket --cors-configuration '{
  "CORSRules": [
    {
      "AllowedOrigins": ["*"],
      "AllowedMethods": ["POST", "PUT", "GET", "HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag", "Location"],
      "MaxAgeSeconds": 3000
    }
  ]
}'
echo "==> CORS configured for direct browser uploads."
