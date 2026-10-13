'use client';

import { useCallback, useState } from 'react';
import { Toast } from './ui';

export function useToast() {
  const [toast, setToast] = useState<{ message: string; tone: 'ok' | 'err' }>({
    message: '',
    tone: 'ok',
  });

  const show = useCallback((message: string, tone: 'ok' | 'err' = 'ok') => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: '', tone: 'ok' }), 3000);
  }, []);

  const node = <Toast message={toast.message} tone={toast.tone} />;
  return { show, node };
}
