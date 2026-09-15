ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages pyserial paho-mqtt==1.6.1

COPY autoterm_protocol.py autoterm_addon.py /app/
COPY run.sh /

RUN chmod a+x /run.sh

WORKDIR /app
CMD [ "/run.sh" ]
