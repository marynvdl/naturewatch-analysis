#!/bin/bash

echo "1. Starting deploy.sh"
echo "Current user: $(whoami)"
echo "Creating directory..."
rm -rf ~/vm_data
mkdir -p ~/vm_data
echo "Directory created. Checking..."
ls -la ~/

# Stop and remove the Docker container if it exists
echo "2. Stopping and removing existing docker container"
docker rm -f create-crop-tiles || true

# Build docker image and run container
echo "3. Create and run docker container"
echo "Checking if ~/key.json exists..."
ls -la ~/

docker build --no-cache -t create-crop-tiles:1.0.0 .
docker run -v ~/key.json:/tmp/keys/key.json -v ~/vm_data:/container/data -d --name create-crop-tiles create-crop-tiles:1.0.0

echo "4. Done with deploy.sh"
