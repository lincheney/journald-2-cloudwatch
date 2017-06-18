# journald-2-cloudwatch
Send journald logs to AWS CloudWatch

[![Build Status](https://travis-ci.org/lincheney/journald-2-cloudwatch.svg?branch=master)](https://travis-ci.org/lincheney/journald-2-cloudwatch)
[![codecov](https://codecov.io/gh/lincheney/journald-2-cloudwatch/branch/master/graph/badge.svg)](https://codecov.io/gh/lincheney/journald-2-cloudwatch)
[![Docker Build Status](https://img.shields.io/docker/build/lincheney/journald-2-cloudwatch.svg)](https://hub.docker.com/r/lincheney/journald-2-cloudwatch/)
[![](https://images.microbadger.com/badges/image/lincheney/journald-2-cloudwatch.svg)](https://microbadger.com/images/lincheney/journald-2-cloudwatch "Get your own image badge on microbadger.com")

Available on Docker Hub: https://hub.docker.com/r/lincheney/journald-2-cloudwatch/

This is heavily based on https://github.com/arkenio/journald-wrapper.

## Usage

```
usage: main.py [-h] -c CURSOR [--logs LOGS] -g LOG_GROUP_FORMAT -s
               LOG_STREAM_FORMAT
optional arguments:
  -h, --help            show this help message and exit
  -c CURSOR, --cursor CURSOR
                        Store/read the journald cursor in this file
  --logs LOGS           Directory to journald logs (default: /var/log/journal)  
  -g LOG_GROUP_FORMAT, --log-group-format LOG_GROUP_FORMAT
                        Python format string for log group names  
  -s LOG_STREAM_FORMAT, --log-stream-format LOG_STREAM_FORMAT
                        Python format string for log stream names
```

Note that the cursor, log group format and log stream format arguments are mandatory.

## Running in Docker

```bash
docker run -v /var/log/journal/:/var/log/journal/:ro -v /data/journald:/data/journald/:rw lincheney/journald-2-cloudwatch -c ... -g ... -s ...
```

If journald is configured with `volatile storage` you should mount `/run/log/journal` instead:

```bash
docker run -v /run/log/journal/:/var/log/journal/:ro -v /data/journald:/data/journald/:rw lincheney/journald-2-cloudwatch -c ... -g ... -s ...
```

The image is based on `debian:jessie-slim`.

## Journal cursor

The journal cursor is stored in the file specified in the `--cursor` flag.
This file should be persisted to disk/placed in a mounted volume; consider using named volumes.
If not, the process may upload duplicate logs if it is ever restarted.

## CloudWatch log format

The log group and stream names for each message are configurable via the corresponding command line parameters. They take a **variant** of [Python format strings](https://docs.python.org/3/library/string.html#formatspec) and are evaluated against each message, the fields from the EC2 [instance identity document](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html) and a few custom fields.

For example, running with the flags `-g '{SYSLOG_IDENTIFIER|"other"}' -s '{$instanceId} - {$region}'` will make log groups named after the syslog identifier (defaulting to "other" if there isn't one) and stream names with the instance id and region.

### Format spec

The [Python format strings spec](https://docs.python.org/3/library/string.html#formatspec) holds with these exceptions/additions:
* non-keyword fields won't work e.g. `{}` and `{3}`. (They are "syntactically valid", they just won't work.)
* fallbacks can be specified with using pipe `|` e.g. `{a|b|c}` will give the first of `a`, `b`, `c` that exist.
* string literals surrounded by " or ' can be used. They cannot contain characters that have a special meaning in the format spec (including `|`). For example, you could have `{a|b|c|"other"}`.

### Fields

The following fields are available to use in the format strings:
* any fields in the journald entry. You can view examples by running `journalctl -o json`.
* any fields in the [instance identity document](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-identity-documents.html), prefixed with `$`, e.g. `$privateIp`. Any fields that are `null` are removed.
* `$unit` which is the same as `USER_UNIT` or `_SYSTEMD_UNIT`, if they exist, but with templating removed (i.e. `sshd@1234.service` becomes `sshd.service`).
* `$docker_container` which is the same as `{CONTAINER_NAME}.container` iff `_SYSTEMD_UNIT` is `docker.service`.

To (almost) replicate the old behaviour of the log stream name, you could use:
```
{$docker_container|$unit|SYSLOG_IDENTIFIER|_EXE|"other"}
```
