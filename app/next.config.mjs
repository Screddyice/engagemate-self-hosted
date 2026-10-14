/** @type {import('next').NextConfig} */
const nextConfig = {
  // Not "standalone": its file tracer fails to emit the client-reference
  // manifest for a page at the root of a route group, which is where the
  // marketing homepage lives. The tracer aborts that page's whole dependency
  // copy with only a warning, so the container builds clean and then 500s on
  // "/" with an undefined clientModules. Running the full build sidesteps it.
  // Lint runs in CI, not as a deploy gate. Production container builds should
  // not fail on stylistic ESLint rules. TypeScript type-checking still gates the build.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
