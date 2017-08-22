FROM debian:jessie-slim

# Install Python, pip, boto3 and python-systemd.
RUN BUILD_DEPS="curl python3-dev pkg-config gcc git libsystemd-journal-dev" \
    VERSION="233"; \
    apt-get update && \
    apt-get install -y python3 $BUILD_DEPS && \
    curl --fail 'https://bootstrap.pypa.io/get-pip.py' | python3 && \
    pip3 install --no-cache-dir boto3 "git+https://github.com/systemd/python-systemd.git/@v$VERSION#egg=systemd" && \
    apt-get purge --auto-remove -y $BUILD_DEPS && \
    pip3 uninstall --yes pip && \
    rm -rf /var/lib/apt/lists/*

COPY main.py /main.py
ENTRYPOINT ["python3", "/main.py"]
