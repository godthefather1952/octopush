FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

USER root

# Octopush does not use Yarn.
# Remove the stale Yarn APT source so Dev Container Features can run apt-get
# successfully while installing Docker-in-Docker.
RUN rm -f \
      /etc/apt/sources.list.d/yarn.list \
      /etc/apt/sources.list.d/yarn.sources \
    && if [ -f /etc/apt/sources.list ]; then \
         sed -i '\|dl.yarnpkg.com|d' /etc/apt/sources.list; \
       fi

USER vscode
