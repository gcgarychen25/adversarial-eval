FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    jq \
    ripgrep \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Per-run evidence is copied into environment/evidence/ before `harbor run`.
COPY evidence /evidence
RUN mkdir -p /evidence/out && chmod -R a+rwX /evidence /evidence/out
