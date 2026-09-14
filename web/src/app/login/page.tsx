import { redirect } from 'next/navigation';

import LoginForm from '../../components/LoginForm';
import { currentUser } from '../../lib/auth';

export const dynamic = 'force-dynamic';

export default async function LoginPage() {
  if (await currentUser()) redirect('/');
  return <LoginForm />;
}
