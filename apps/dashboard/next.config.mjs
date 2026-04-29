/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The Python API runs at NEXT_PUBLIC_API_URL (default localhost:8000);
  // we don't proxy in production - the API is reverse-proxied at infra
  // layer.  In dev, set NEXT_PUBLIC_API_URL=http://localhost:8000.
  experimental: {
    optimizePackageImports: ["lucide-react", "recharts"],
  },
};

export default nextConfig;
