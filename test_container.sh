#!/bin/bash
set -e

IMAGE="tunnelsats/umbrel-app:test"

echo "Building local test image..."
docker build -t $IMAGE .

echo "Testing dependencies via container..."
declare -a cmds=(
    "python3 --version"
    "wg --version"
    "curl --version"
    "jq --version"
    "python3 -c \"import flask; print('Flask found!')\""
    "python3 -c \"import requests; print('Requests found!')\""
)

for cmd in "${cmds[@]}"; do
    echo ">> Checking: $cmd"
    docker run --rm --entrypoint="" $IMAGE sh -c "$cmd"
done

echo "All structure tests passed successfully!"
