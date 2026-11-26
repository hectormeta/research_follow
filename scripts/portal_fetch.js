#!/usr/bin/env node
/**
 * 校内门户（清华 SIGS WebVPN）无头登录与首页抓取子进程。
 *
 * 调用方式: node portal_fetch.js <outHtmlFile>
 * 凭据: 环境变量 PORTAL_USER / PORTAL_PASS 优先；缺失回退 ~/.config/research_follow/portal.json。
 *       凭据只经环境变量/文件传入，不进命令行参数、不回显到 stdout/stderr。
 *
 * 退出码:
 *   0  成功，HTML 已写入 outHtmlFile，stdout 末行 `RESULT items=<n>`
 *   2  缺凭据
 *   3  认证失败（账号密码错误 / 触发图形验证码 / 短信 OTP）
 *   4  多轮重试后仍未进入首页（会话未落地 / 网络 / 上游 502）
 *   1  其他异常
 *
 * 说明: 首页卡片为 React onClick 路由、无 href；列表仅含标题 + MM-DD 发布日期，
 *       申请/项目类截止日期在详情正文，留待详情接口校准。上游 WebVPN 间歇 502、
 *       后端 cookie 异步同步，故内部做多轮登录重试。
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

function loadCred() {
  let u = (process.env.PORTAL_USER || '').trim();
  let p = (process.env.PORTAL_PASS || '').trim();
  if (u && p) return { username: u, password: p };
  try {
    const f = path.join(os.homedir(), '.config', 'research_follow', 'portal.json');
    const d = JSON.parse(fs.readFileSync(f, 'utf-8'));
    return { username: String(d.username || '').trim(), password: String(d.password || '').trim() };
  } catch (e) {
    return { username: '', password: '' };
  }
}

function pickBrowser() {
  if (process.env.PORTAL_BROWSER && fs.existsSync(process.env.PORTAL_BROWSER)) {
    return process.env.PORTAL_BROWSER;
  }
  const candidates = [
    '/opt/vm/preinstall/ms-playwright/chromium_headless_shell-1169/chrome-linux/headless_shell',
    '/opt/vm/preinstall/ms-playwright/chromium-1169/chrome-linux/chrome',
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return undefined; // 让 playwright 使用其自带浏览器
}

const DEFAULT_START = 'https://webvpn.sigs.tsinghua.edu.cn/https/77726476706e69737468656265737421e0f85388263c2643770f9ae28c462a3b9c03027879bfa2a49d2feb/sigsPortal/home?org=&page=page_group_teacher_home&tab=news&top_external_id=top_portal_manage&type=fs_info&lang=zh';

async function submitLogin(page, cred, debug) {
  await page.waitForSelector('.user .userinput input', { timeout: 5000 }).catch(() => {});
  await page.fill('.user .userinput input', cred.username).catch(() => {});
  await page.fill('.user .passwordinput input', cred.password).catch(() => {});
  let authCode = null;
  const onAuth = page.waitForResponse(
    r => r.url().includes('authenticate'), { timeout: 15000 }).catch(() => null);
  await page.click('#loginbtn').catch(() => {});
  const resp = await onAuth;
  if (resp) { try { authCode = (await resp.json()).code; } catch (e) { authCode = null; } }
  if (debug) console.error(`[dbg login] authCode=${authCode}`);
  await page.waitForTimeout(1500);
  const redirect = await page.evaluate(
    () => document.getElementById('redirectUri')?.value || ''
  ).catch(() => '');
  if (redirect) await page.evaluate(u => { location.href = u; }, redirect).catch(() => {});
  return authCode;
}

async function attempt(browser, cred, START, debug) {
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true,
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
  });
  const page = await ctx.newPage();
  let cards = 0, html = '', authCode = null;
  try {
    await page.goto(START, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1500);

    const has = async sel => { try { return !!(await page.$(sel)); } catch (e) { return false; } };
    const dismissTrustLayer = async () => {
      for (const sel of ['a.layui-layer-btn0', 'a.layui-layer-btn1']) {
        if (await has(sel)) { await page.click(sel).catch(() => {}); await page.waitForTimeout(500); return; }
      }
    };
    const log = async tag => {
      if (!debug) return;
      const s = await page.evaluate(() => ({
        url: location.href.slice(0, 80),
        text: document.body ? document.body.innerText.replace(/\s+/g, ' ').length : 0,
      })).catch(() => ({ url: page.url().slice(0, 80), text: 0 }));
      console.error(`[dbg ${tag}] url=${s.url} form=${await has('.user .userinput input')} text=${s.text}`);
    };

    if (await has('.user .userinput input')) {
      authCode = await submitLogin(page, cred, debug);
    }
    await log('after-login');

    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(3000);
      await dismissTrustLayer();
      cards = await page.$$eval('a.LpEu0_0e span', els => els.length).catch(() => 0);
      await log('poll' + i);
      if (cards > 0) break;
      if (await has('.user .userinput input')) {
        authCode = await submitLogin(page, cred, debug);
      }
    }
    if (cards > 0) html = await page.content();
    return { cards, html, authCode };
  } finally {
    await ctx.close().catch(() => {});
  }
}

async function main() {
  const outFile = process.argv[2];
  if (!outFile) { console.error('usage: node portal_fetch.js <outHtmlFile>'); process.exit(1); }

  const cred = loadCred();
  if (!cred.username || !cred.password) process.exit(2);

  let chromium;
  try { ({ chromium } = require('playwright')); }
  catch (e) { console.error('playwright module unavailable'); process.exit(1); }

  const START = process.env.PORTAL_HOME || DEFAULT_START;
  const debug = !!process.env.PORTAL_DEBUG;
  const browser = await chromium.launch({
    executablePath: pickBrowser(),
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });

  const ROUNDS = 3;
  try {
    for (let round = 1; round <= ROUNDS; round++) {
      if (debug) console.error(`[dbg] round ${round}`);
      const res = await attempt(browser, cred, START, debug);
      if (res.cards > 0) {
        fs.writeFileSync(outFile, res.html, 'utf-8');
        console.log(`RESULT items=${res.cards}`);
        return;
      }
      // 明确的认证失败（非 null 且非成功码）才终止；null 归为上游/会话问题，继续重试
      if (res.authCode !== null && String(res.authCode) !== '100000001') {
        process.exit(3);
      }
      if (round < ROUNDS) await page_sleep(5000);
    }
    process.exit(4);
  } finally {
    await browser.close().catch(() => {});
  }
}

function page_sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

main().catch(e => {
  console.error('portal fetch failed: ' + String(e && e.message || e).slice(0, 160));
  process.exit(1);
});
