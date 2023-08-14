#!/bin/bash
mkdir -p ~/vm_data

# Stop and remove the Docker container if it exists
docker rm -f create-built-tiles || true

# Build docker image and run container
docker build --no-cache -t create-built-tiles:1.0.9 .
docker run -v ~/key.json:/tmp/keys/key.json -v ~/vm_data:/container/data -d --name create-built-tiles create-built-tiles:1.0.9