#!/bin/bash
set -e

IMAGE="tunnelsats/umbrel-app:test"

echo "Building local test image..."
docker build -t $IMAGE .

echo "Testing if python3 is installed..."
docker run --rm --entrypoint="" $IMAGE python3 --version

echo "Testing if wireguard is installed..."
docker run --rm --entrypoint="" $IMAGE wg --version

echo "Testing if curl is installed..."
docker run --rm --entrypoint="" $IMAGE curl --version

echo "Testing if jq is installed..."
docker run --rm --entrypoint="" $IMAGE jq --version

echo "Testing if flask is available..."
docker run --rm --entrypoint="" $IMAGE python3 -c "import flask; print('Flask found!')"

echo "Testing if requests is available..."
docker run --rm --entrypoint="" $IMAGE python3 -c "import requests; print('Requests found!')"

echo "All structure tests passed successfully!"
