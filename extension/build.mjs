import esbuild from 'esbuild';
import { copyFileSync, mkdirSync } from 'fs';

const watch = process.argv.includes('--watch');

mkdirSync('dist', { recursive: true });

/** @type {esbuild.BuildOptions} */
const options = {
  entryPoints: [
    'src/background.js',
    'src/offscreen.js',
    'src/content.js',
    'src/popup.js',
  ],
  bundle: true,
  format: 'iife',
  outdir: 'dist',
  logLevel: 'info',
};

// 静态文件直接拷贝
for (const f of ['manifest.json', 'offscreen.html', 'popup.html', 'overlay.css', 'pcm-worklet.js']) {
  copyFileSync(`src/${f}`, `dist/${f}`);
}

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}
