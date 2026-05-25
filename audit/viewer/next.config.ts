import type { NextConfig } from "next";

const config: NextConfig = {
  // The viewer talks to the core FastAPI backend. Set OC_API_BASE at
  // build time (or via .env.local) to the tailnet HTTPS URL.
  // e.g. OC_API_BASE=https://rky-server-cn.tail19ad89.ts.net:8000
  env: {
    OC_API_BASE: process.env.OC_API_BASE ?? "http://localhost:8000",
  },
};

export default config;
