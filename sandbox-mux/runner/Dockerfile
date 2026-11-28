# Agent runtime image for the sandboxes. Build it, push it to a registry, then turn it into a
# sandbox disk image once per release:
#
#   docker build -t myregistry.azurecr.io/agent:1.4 -f runner/Dockerfile runner
#   docker push myregistry.azurecr.io/agent:1.4
#   python -m sandbox_mux.image build --image myregistry.azurecr.io/agent:1.4 \
#          --name agent-1-4 --managed-identity system        # prints {"id": "..."}
#   SANDBOX_MUX_DISK_ID=<id>                                   # then roll the service
#
# The service needs these in the image: useradd, runuser, chgrp, install, find, tar, timeout,
# pkill (procps), and an exec user that is root. Everything under /opt/agent must be root-owned
# and not writable by users, because every user on a sandbox runs it.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends procps util-linux passwd coreutils findutils tar \
 && rm -rf /var/lib/apt/lists/*

# Dependencies. Add your agent's requirements to runner/requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && python -m compileall -q /usr/local/lib/python3.12/site-packages   # bytecode built once, as root

COPY run_turn.py /opt/agent/run_turn.py
RUN python -m compileall -q /opt/agent && chmod -R go-w /opt/agent

# A sandbox built from an image runs the image's command. The Python image's default is `python3`,
# which exits at once without a terminal, so keep the sandbox alive. (Or set pool.entrypoint / pool.cmd.)
CMD ["sleep", "infinity"]
