import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Only these two public values reach the browser bundle, read from the root
// .env in development and from Docker build arguments in the image.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, "..", "SUPABASE_"), ...process.env };
  // This value ships to every visitor, so refuse a secret key put here by mistake.
  const key = env.SUPABASE_PUBLISHABLE_KEY ?? "";
  const jwtRole = key.startsWith("eyJ") ? JSON.parse(Buffer.from(key.split(".")[1] ?? "", "base64url").toString() || "{}").role : null;
  if (key.startsWith("sb_secret_") || jwtRole === "service_role") {
    throw new Error("SUPABASE_PUBLISHABLE_KEY holds a secret key. Use the publishable key.");
  }
  return {
    plugins: [react()],
    // Serve every font as a file: the Content-Security-Policy refuses data: fonts.
    build: { assetsInlineLimit: 0 },
    define: {
      __SUPABASE_URL__: JSON.stringify(env.SUPABASE_URL),
      __SUPABASE_PUBLISHABLE_KEY__: JSON.stringify(env.SUPABASE_PUBLISHABLE_KEY),
    },
    server: {
      proxy: {
        "/api": { target: "http://127.0.0.1:8000", rewrite: (path) => path.replace(/^\/api/, "") },
      },
    },
  };
});
