/** @type {import('next').NextConfig} */
const config = {
  // argon2 is a native module: it must stay in the Node runtime and never be
  // bundled. The auth routes also declare `runtime = 'nodejs'` for the same
  // reason.
  serverExternalPackages: ['@node-rs/argon2', '@prisma/client'],
  // The production image runs the self-contained server Next traces into
  // .next/standalone, not a full node_modules (see Dockerfile).
  output: 'standalone',
};

export default config;
