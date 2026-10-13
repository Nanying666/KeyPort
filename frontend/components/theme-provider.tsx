'use client';

import { createContext, useContext } from 'react';

/**
 * 深色单主题：控制室方向不做浅色切换（见 .interface-design/system.md）。
 * 保留 Provider 形态，避免改动既有调用点；theme 恒为 dark，toggle 为 no-op。
 */
type Theme = 'dark';

interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx>({ theme: 'dark', toggle: () => {} });

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return <Ctx.Provider value={{ theme: 'dark', toggle: () => {} }}>{children}</Ctx.Provider>;
}

export const useTheme = () => useContext(Ctx);
