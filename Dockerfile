FROM python:3.13-alpine AS builder

ARG APP_WORKDIR=/iptv-api

WORKDIR $APP_WORKDIR

COPY Pipfile* ./

RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev zlib-dev jpeg-dev wget make pcre-dev \
  && pip install pipenv \
  && PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM python:3.13-alpine

ARG APP_WORKDIR=/iptv-api

ENV APP_WORKDIR=$APP_WORKDIR
ENV PATH="$APP_WORKDIR/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

WORKDIR $APP_WORKDIR

COPY . $APP_WORKDIR

COPY --from=builder $APP_WORKDIR/.venv $APP_WORKDIR/.venv

RUN apk add --no-cache ffmpeg

COPY entrypoint.sh /iptv-api-entrypoint.sh

COPY config /iptv-api-config

RUN chmod +x /iptv-api-entrypoint.sh

ENTRYPOINT ["/iptv-api-entrypoint.sh"]
