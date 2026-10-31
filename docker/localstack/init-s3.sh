#!/usr/bin/env bash
# Runs inside LocalStack when it is ready: create buckets and a small demo dataset.
set -euo pipefail

awslocal s3 mb s3://rws-data
awslocal s3 mb s3://rws-archive

tmp=$(mktemp -d)
printf 'sample_id,temperature\n1,21.5\n2,22.1\n3,20.9\n' > "$tmp/measurements.csv"
printf '{"instrument": "spectrometer-01", "units": "celsius"}\n' > "$tmp/metadata.json"
printf 'Demo dataset for the Research Workflow Service.\n' > "$tmp/README.txt"
awslocal s3 cp "$tmp" s3://rws-data/datasets/demo/ --recursive
rm -rf "$tmp"
echo "rws: buckets and demo dataset ready"
