# syntax=docker/dockerfile:1
ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:25.06-py3
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd

# Host keys and authorized_keys are generated per CCI by SLAI-tool.
# No credentials, personal public keys or running daemons are baked in.
EXPOSE 22
