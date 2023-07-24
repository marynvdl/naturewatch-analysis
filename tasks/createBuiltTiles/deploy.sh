#!/bin/bash

# Stop and remove the Docker container if it exists
docker rm -f create-built-tiles || true

# Build docker image and run container
docker build --no-cache -t create-built-tiles:1.0.9 .
docker run -v /etc/keys:/tmp/keys -d --name create-built-tiles -v /mnt/disks/dataDisk:/container/data create-built-tiles:1.0.9
