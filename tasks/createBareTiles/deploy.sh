#!/bin/bash

docker build --no-cache -t create-bare-tiles:1.0.0 .
docker run -v /etc/keys:/tmp/keys -d --name create-bare-tiles -v /mnt/disks/dataDisk:/container/data create-bare-tiles:1.0.0
