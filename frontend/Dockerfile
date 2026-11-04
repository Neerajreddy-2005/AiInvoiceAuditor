# frontend: static build served by unprivileged nginx, which also proxies /api
# to the agent, so the browser only ever talks to one origin.
FROM node:22.21.1-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Public values: both are designed to be visible in the browser.
ARG SUPABASE_URL
ARG SUPABASE_PUBLISHABLE_KEY
ENV SUPABASE_URL=$SUPABASE_URL SUPABASE_PUBLISHABLE_KEY=$SUPABASE_PUBLISHABLE_KEY
RUN npm run build

# Republishes the built site to Netlify when a public demo starts (see compose.yaml).
FROM build AS publisher
RUN apk upgrade --no-cache && npm install -g netlify-cli@27.16.2 && chown node:node /app && chown -R node:node /app/dist
USER node
CMD ["node", "scripts/publish-demo.mjs"]

FROM nginxinc/nginx-unprivileged:1.29.3-alpine
# Security fixes newer than the base image, then back to its unprivileged user.
USER root
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -q --spider http://127.0.0.1:8080/ || exit 1
