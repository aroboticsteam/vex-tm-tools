import type { ReactNode } from 'react';
import { Navbar } from './Navbar';
import { ReconnectingBanner } from '../ws/ReconnectingBanner';
import { RedisWarningBanner } from './RedisWarningBanner';
import { ToastContainer } from '../ui/ToastContainer';

export function PageLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen overflow-x-hidden bg-vmd-bg pb-16">
      <Navbar />
      <ReconnectingBanner />
      <RedisWarningBanner />
      <main className="mx-3 mt-4 sm:mx-4 sm:mt-6 lg:mx-8">{children}</main>
      <ToastContainer />
    </div>
  );
}
