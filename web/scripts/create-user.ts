/* Create or update a login.
 *
 *   npx tsx scripts/create-user.ts <username> <password>
 *
 * There is no public sign-up route. Accounts are made here deliberately: a
 * publicly reachable app with open registration is an open invitation to queue
 * CPU work on a two-core box.
 */
import { hashPassword } from '../src/lib/auth';
import { prisma } from '../src/lib/db';
import { randomUUID } from 'node:crypto';

const [username, password] = process.argv.slice(2);

if (!username || !password) {
  console.error('usage: npx tsx scripts/create-user.ts <username> <password>');
  process.exit(1);
}
if (password.length < 10) {
  console.error('password must be at least 10 characters');
  process.exit(1);
}

// Wrapped rather than top-level await: this file has no "type": "module", so
// tsx emits CommonJS and top-level await is a syntax error there.
async function main() {
  const hash = await hashPassword(password);
  const existing = await prisma.users.findUnique({ where: { username } });

  if (existing) {
    await prisma.users.update({ where: { id: existing.id }, data: { password_hash: hash } });
    console.log(`updated password for ${username}`);
  } else {
    await prisma.users.create({
      data: { id: randomUUID(), username, password_hash: hash },
    });
    console.log(`created ${username}`);
  }
  await prisma.$disconnect();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
