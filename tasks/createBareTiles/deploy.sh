#!/bin/bash

# Stop & remove the Docker container if it exists
docker rm -f create-bare-tiles || true

# Build docker image and run container
docker build --no-cache -t create-bare-tiles:1.0.0 .
docker run -v /etc/keys:/tmp/keys -d --name create-bare-tiles -v /mnt/disks/dataDisk/task2:/container/data create-bare-tiles:1.0.0
