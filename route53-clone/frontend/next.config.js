/** @type {import("next").NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "https://route53-clone-ocjr.onrender.com/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
