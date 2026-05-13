/** @type {import('next').NextConfig} */
// On Vercel, default to the Railway production backend; locally default to localhost.
const apiTarget =
  process.env.BACKEND_URL ||
  (process.env.VERCEL
    ? 'https://autotrade-backend-production.up.railway.app'
    : 'http://localhost:8000');

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
