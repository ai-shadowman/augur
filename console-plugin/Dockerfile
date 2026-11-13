FROM registry.access.redhat.com/ubi9/nodejs-22:latest AS build
USER root
WORKDIR /usr/src/app

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# 1. Isolated dependency layer: cached unless package.json or package-lock.json changes
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then \
      npm ci --prefer-offline --loglevel=error; \
    else \
      npm install --prefer-offline --loglevel=error; \
    fi

# 2. Copy only webpack build inputs so nginx.conf or Dockerfile edits do not re-trigger builds
COPY tsconfig.json webpack.config.ts console-extensions.json ./
COPY locales ./locales
COPY src ./src
RUN npm run build

FROM registry.access.redhat.com/ubi9/nginx-120:latest
# Static nginx config in its own cached layer before compiled assets
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=1001:0 /usr/src/app/dist /usr/share/nginx/html
USER 1001
EXPOSE 9443
ENTRYPOINT ["nginx", "-g", "daemon off;"]
