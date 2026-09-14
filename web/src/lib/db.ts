import { PrismaClient } from '@prisma/client';

/* A single Prisma client for the process.
 *
 * Next dev reloads modules on every edit; without this guard each reload opens
 * a fresh pool and the database runs out of connections after a few minutes of
 * editing. The global is the documented workaround.
 */
const globalForPrisma = globalThis as unknown as { prisma?: PrismaClient };

export const prisma =
  globalForPrisma.prisma ??
  new PrismaClient({
    log: process.env.NODE_ENV === 'development' ? ['warn', 'error'] : ['error'],
  });

if (process.env.NODE_ENV !== 'production') globalForPrisma.prisma = prisma;
