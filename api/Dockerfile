# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/api:/srv

ARG GOOGLE_LINUX_SIGNING_KEY_FINGERPRINT=EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl gnupg \
    && curl --fail --silent --show-error --location \
        https://dl.google.com/linux/linux_signing_key.pub \
        --output /tmp/google-linux-signing-key.pub \
    && test "$(gpg --show-keys --with-colons /tmp/google-linux-signing-key.pub \
        | awk -F: '$1 == "fpr" { print $10; exit }')" \
        = "$GOOGLE_LINUX_SIGNING_KEY_FINGERPRINT" \
    && gpg --dearmor --yes \
        --output /usr/share/keyrings/google-chrome.gpg \
        /tmp/google-linux-signing-key.pub \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends google-chrome-stable \
    && apt-get purge --yes curl gnupg \
    && apt-get autoremove --yes \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/google-linux-signing-key.pub

COPY api/requirements.txt /tmp/api-requirements.txt
RUN pip install --requirement /tmp/api-requirements.txt

COPY api /srv/api
COPY deploy /srv/deploy
# QA promotion uses umask 0077; the login service runs as an unprivileged user.
RUN chmod -R a+rX /srv/api /srv/deploy

WORKDIR /srv/api
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
