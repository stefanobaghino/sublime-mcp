# Headless Sublime Text image with plugin.py preloaded.
#
# Boots Xvfb on :1, launches `subl --stay /work`, and lets
# plugin_loaded() bind sublime-mcp's HTTP server on 127.0.0.1:47823
# inside the container. A volume can be mounted at /work.

# Pinned by digest so a silent base-image republication doesn't
# invalidate every downstream layer. Bump intentionally.
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    DISPLAY=:1

# GTK + locales are the same dependency set SublimeText/UnitTesting's
# Docker image uses for headless ST. Xvfb provides the virtual display;
# psmisc gives `pkill` (used by the entrypoint's shutdown trap).
#
# The sed swaps the deb822 sources to azure.archive.ubuntu.com: GitHub's
# Linux runners are Azure VMs and the runner OS itself already points
# there, so colocated traffic is far faster and more reliable than the
# public archive/security mirrors (which have stalled this build for
# >15 min on retry storms).
RUN sed -i 's|http://archive.ubuntu.com|http://azure.archive.ubuntu.com|g; s|http://security.ubuntu.com|http://azure.archive.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        libglib2.0-0 \
        libgtk-3-0 \
        locales \
        locales-all \
        psmisc \
        xvfb \
 && rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8

# Sublime HQ's official apt repo (stable channel). Avoids the
# scrape-the-download-page dance and keeps version selection in
# upstream's hands.
RUN install -d /etc/apt/keyrings \
 && curl -fsSL https://download.sublimetext.com/sublimehq-pub.gpg \
      | gpg --dearmor -o /etc/apt/keyrings/sublimehq-archive.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/sublimehq-archive.gpg] https://download.sublimetext.com/ apt/stable/" \
      > /etc/apt/sources.list.d/sublime-text.list \
 && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 update \
 && apt-get install -y --no-install-recommends sublime-text \
 && rm -rf /var/lib/apt/lists/*

# ST's per-user state goes under $HOME. We run as root inside the
# container; that's fine for an ephemeral, single-tenant sandbox.
RUN mkdir -p /root/.config/sublime-text/Packages/User
COPY plugin.py /root/.config/sublime-text/Packages/User/plugin.py
# Pin User-package plugins to ST's Python 3.8 host. Without this, ST's
# Linux build routes the User package to the 3.3 host, where
# `from http.server import ThreadingHTTPServer` (added in 3.7) fails
# to import silently and `plugin_loaded()` is never called.
RUN echo "3.8" > /root/.config/sublime-text/Packages/User/.python-version

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 47823

WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
