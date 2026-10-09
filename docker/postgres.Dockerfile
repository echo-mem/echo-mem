# Postgres + pgvector + Apache AGE.
#
# Built from pgvector/pgvector:pg16 (multi-arch: amd64 and arm64) rather than
# the apache/age image, which only publishes amd64. PR0a's spike found that
# apache/age under QEMU emulation on Apple Silicon returned traversal
# latencies 25-140x slower than native and produced a false NO-GO; compiling
# AGE from source on a native-arch base avoids that tax on every architecture
# instead of just working around it for local dev.
# Pinned by digest, not by tag. A tag is a moving target and this one is the
# base of the database every customer's facts live in, so "pg16" meaning
# something different next week is not a theoretical supply chain concern.
#
# This digest is the OCI image INDEX, not a single architecture, so the
# multi-arch property the comment above depends on is preserved: amd64 and
# arm64 both resolve from it, and production is arm64 on Graviton.
#
# To move it deliberately:
#   docker buildx imagetools inspect pgvector/pgvector:pg16
FROM pgvector/pgvector:pg16@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        postgresql-server-dev-16 \
        flex \
        bison \
        libreadline-dev \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --branch release/PG16/1.5.0 --depth 1 https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config \
    && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config install \
    && rm -rf /tmp/age
