/** @type {import('next').NextConfig} */
const apiTarget = process.env.BACKEND_URL || 'http://localhost:8000';

const nextConfig = {
  output: 'standalone',
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${apiTarget}/api/:path*` },
      { source: '/ws/:path*',  destination: `${apiTarget}/ws/:path*` },
    ];
  },
};

module.exports = nextConfig;
