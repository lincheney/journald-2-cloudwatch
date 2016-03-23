FROM base/archlinux

# update keys
RUN pacman --noconfirm -Sy archlinux-keyring

# install deps
RUN pacman -Sy --noconfirm openssl python-pip python-systemd && \
    pip install boto3 && \
    pacman -Rs --noconfirm python-pip && \
    pacman -Scc --noconfirm

COPY main.py /main.py
ENTRYPOINT ["python", "/main.py"]
