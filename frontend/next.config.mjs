/** @type {import('next').NextConfig} */
const nextConfig = {
  // 静态导出：产物交由后端 FastAPI 托管，单进程单端口部署
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
  reactStrictMode: true,
};

export default nextConfig;
