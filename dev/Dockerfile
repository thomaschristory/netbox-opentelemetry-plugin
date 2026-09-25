ARG NETBOX_IMAGE=docker.io/netboxcommunity/netbox:v4.7-5.1.1
FROM ${NETBOX_IMAGE}

USER root
COPY pyproject.toml README.md /plugin/
COPY netbox_opentelemetry_plugin /plugin/netbox_opentelemetry_plugin
# Editable install: the source directory is also bind-mounted by compose, so code edits apply on restart.
RUN /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python -e /plugin

# Extra web servers for the fork-safety profiles (dev only; not plugin dependencies).
RUN /usr/local/bin/uv pip install --python /opt/netbox/venv/bin/python gunicorn==26.2.0 pyuwsgi==2.0.30.post1
