FROM registry.access.redhat.com/ubi9/nodejs-22:latest AS build
USER root
WORKDIR /usr/src/app
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM registry.access.redhat.com/ubi9/nginx-120:latest
COPY --from=build /usr/src/app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
USER 1001
EXPOSE 9443
ENTRYPOINT ["nginx", "-g", "daemon off;"]
