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

FROM nginxinc/nginx-unprivileged:1.29.3-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -q --spider http://127.0.0.1:8080/ || exit 1
