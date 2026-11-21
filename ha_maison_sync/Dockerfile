ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache git openssh-client python3 py3-yaml

COPY run.sh /run.sh
COPY app /app
RUN chmod a+x /run.sh

CMD ["/run.sh"]
