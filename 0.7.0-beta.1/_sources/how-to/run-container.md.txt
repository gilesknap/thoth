# Run in a container

Pre-built containers with thoth and its dependencies already
installed are available on [Github Container Registry](https://ghcr.io/gilesknap/thoth).

## Starting the container

To pull the container from github container registry and run:

```
$ docker run ghcr.io/gilesknap/thoth:latest --version
```

To get a released version, use a numbered release instead of `latest`.
