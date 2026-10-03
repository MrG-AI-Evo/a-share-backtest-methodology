import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
