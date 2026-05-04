# Headless Sublime Text image with sublime_mcp.py preloaded.
#
# Boots Xvfb on :1, launches `subl --stay /work`, and lets
# plugin_loaded() bind sublime-mcp's HTTP server on 127.0.0.1:47823
# inside the container. A volume can be mounted at /work.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    DISPLAY=:1

# GTK + locales are the same dependency set SublimeText/UnitTesting's
# Docker image uses for headless ST. Xvfb provides the virtual display;
# psmisc gives `pkill` (used by the entrypoint's shutdown trap).
RUN apt-get update \
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
 && apt-get update \
 && apt-get install -y --no-install-recommends sublime-text \
 && rm -rf /var/lib/apt/lists/*

# ST's per-user state goes under $HOME. We run as root inside the
# container; that's fine for an ephemeral, single-tenant sandbox.
RUN mkdir -p /root/.config/sublime-text/Packages/User
COPY sublime_mcp.py /root/.config/sublime-text/Packages/User/sublime_mcp.py
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
