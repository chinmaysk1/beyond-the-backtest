/** @type {import('next').NextConfig} */
const config = {
  // argon2 is a native module: it must stay in the Node runtime and never be
  // bundled. The auth routes also declare `runtime = 'nodejs'` for the same
  // reason.
  serverExternalPackages: ['@node-rs/argon2', '@prisma/client'],
};

export default config;
