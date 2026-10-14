/** @type {import("next").NextConfig} */
const nextConfig = {
  async rewrites() {
    const backendURL =
      process.env.BACKEND_URL ||
      "http://127.0.0.1:8000";

    return [
      {
        source: "/api/:path*",
        destination: `${backendURL}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;