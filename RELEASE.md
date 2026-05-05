#

things todo to perform a release.

can try to automate some of these later.

GitHub - <https://github.com/BuxtonCalvin/MultiProtocolGateway/releases>
PyPi Package - <https://pypi.org/project/multi-protocol-gateway/>

``` ini
pyproject.toml -> version
```

``` ini
python -m build
python -m twine upload dist/*
```

HomeAssistant repo - <https://github.com/BuxtonCalvin/multi-protocol-gateway-hass-addon>

``` ini
https://github.com/BuxtonCalvin/multi-protocol-gateway-hass-addon/blob/master/multi-protocol-gateway/Dockerfile
```

Docker Image - <https://hub.docker.com/r/buxtoncalvin/multiprotocolgateway>

``` ini
wsl
docker login -u buxtoncalvin
```

``` ini
docker pull buxtoncalvin/multiprotocolgateway:latest
docker tag buxtoncalvin/multiprotocolgateway:latest buxtoncalvin/multiprotocolgateway:v1.1.9
docker push buxtoncalvin/multiprotocolgateway:v1.1.9
```

``` ini
docker build -t buxtoncalvin/multiprotocolgateway:latest .
docker push buxtoncalvin/multiprotocolgateway:latest
```
