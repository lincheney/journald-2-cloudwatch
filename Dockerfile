FROM dock0/arch

RUN pacman -Syu --noconfirm --needed python-pip python-systemd && \
    pip install boto3 && \
    pacman -Rs --noconfirm python-pip && \
    pacman -Scc --noconfirm

COPY main.py /main.py
ENTRYPOINT ["python", "/main.py"]
