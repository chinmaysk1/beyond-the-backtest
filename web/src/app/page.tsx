import { redirect } from 'next/navigation';

import Dashboard from '../components/Dashboard';
import { currentUser } from '../lib/auth';

export const dynamic = 'force-dynamic';

export default async function Home() {
  // Checked on the server. A client-side guard only hides the page; the data
  // routes each verify the session independently, which is what actually
  // protects them.
  const user = await currentUser();
  if (!user) redirect('/login');
  return <Dashboard username={user.username ?? 'account'} />;
}
