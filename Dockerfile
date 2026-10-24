FROM scratch

ARG SOURCE_URL
ARG SOURCE_VERSION
ARG REVISION
ARG PLUGIN_NAME
ARG PLUGIN_DOMAIN
ARG ARTIFACT_KIND

LABEL org.opencontainers.image.title="${PLUGIN_NAME}" \
      org.opencontainers.image.description="HACS integration ${PLUGIN_DOMAIN}, packaged as a data-only OCI image" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${SOURCE_VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      io.hacs-docker.plugin="${PLUGIN_NAME}" \
      io.hacs-docker.domain="${PLUGIN_DOMAIN}" \
      io.hacs-docker.artifact-kind="${ARTIFACT_KIND}"

COPY build/plugin/ /
