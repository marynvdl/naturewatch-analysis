# Batch image for the places run: GDAL for reading and proof, felt/tippecanoe for tiles.
# Pinned: Tippecanoe 2.79.0. The Debian tippecanoe package is older and cannot write PMTiles.
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.11.4
ARG TIPPECANOE_VERSION=2.79.0
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential git libsqlite3-dev zlib1g-dev python3-pip python3-venv \
 && git clone --depth 1 --branch ${TIPPECANOE_VERSION} https://github.com/felt/tippecanoe.git /tmp/tippecanoe \
 && make -C /tmp/tippecanoe -j"$(nproc)" \
 && make -C /tmp/tippecanoe install \
 && rm -rf /tmp/tippecanoe /var/lib/apt/lists/*
COPY places/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --break-system-packages -r /tmp/requirements.txt pmtiles
