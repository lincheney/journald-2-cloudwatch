FROM debian:jessie

RUN apt-get update && \
    apt-get install -y python3 python3-systemd && \
    rm -rf /var/lib/apt/lists/*

ADD "https://bootstrap.pypa.io/get-pip.py" /get-pip.py
RUN python3 /get-pip.py && \
    pip3 install boto3

COPY main.py /main.py
ENTRYPOINT ["python3", "/main.py"]
