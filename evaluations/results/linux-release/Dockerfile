FROM node:22-bookworm-slim AS node_tools
FROM docker:27-cli AS docker_tools
FROM python:3.12-slim-bookworm
COPY --from=node_tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node_tools /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=docker_tools /usr/local/bin/docker /usr/local/bin/docker
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends g++ make ca-certificates
WORKDIR /work
COPY . /work
RUN python -m pip install --disable-pip-version-check -q './packages/python[dev,redis]' build \
    -r evaluations/requirements-live.txt \
    && npm --prefix packages/typescript ci --no-audit --no-fund \
    && npm --prefix packages/typescript run build
CMD ["python", "linux_release_check.py"]
