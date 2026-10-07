import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Do not advertise the framework in every response.
  poweredByHeader: false,
};

export default nextConfig;
