import type { Metadata } from 'next';
import { IBM_Plex_Sans, JetBrains_Mono } from 'next/font/google';
import './globals.css';
import { ThemeProvider } from '@/components/theme-provider';
import { AuthProvider } from '@/lib/auth';

/**
 * 字体走 next/font：构建期下载并自托管，避免运行期跨站请求导致的
 * 字体闪烁（FOUT）与布局偏移（CLS）。CSS 变量供 globals.css 引用。
 *
 * latin 子集不含中文字形，中文由 font-family 里的系统字体承接，
 * 因此不下载 full 子集——那会把每个字重撑到数 MB。
 */
const plexSans = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-sans',
  display: 'swap',
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'KeyPort 管理台',
  description: '自托管多账号 API 反向代理网关',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="zh-CN"
      className={`dark ${plexSans.variable} ${jetbrainsMono.variable}`}
      suppressHydrationWarning
    >
      <body>
        <ThemeProvider>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
