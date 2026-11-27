#!/usr/bin/env node
/**
 * 校内门户（清华 SIGS WebVPN）无头登录、首页列表与详情正文抓取子进程。
 *
 * 调用方式: node portal_fetch.js <outJsonFile>
 * 凭据: 环境变量 PORTAL_USER / PORTAL_PASS 优先；缺失回退 ~/.config/research_follow/portal.json。
 *       凭据只经环境变量/文件传入，不进命令行参数、不回显到 stdout/stderr。
 *
 * 输出 JSON 到 outJsonFile:
 *   { "cards": [ { "title", "url", "id", "date", "body" } ] }
 *   stdout 末行 `RESULT items=<n> detail=<m>`
 *
 * 详情: 卡片 data.url 为裸 portal-info 链；经页面 vpn_rewrite_url 转成 WebVPN 包裹链接后，
 *       复用门户会话即可免二次 SSO 读取正文（容器 .wp_articlecontent，回退 article）。
 *       仅抓前 PORTAL_DETAIL_LIMIT 条（默认 10，0 = 不抓详情）；单条失败 body 留空，不阻塞列表。
 *
 * 退出码:
 *   0  成功，JSON 已写入 outJsonFile
 *   2  缺凭据
 *   3  认证失败（账号密码错误 / 触发图形验证码 / 短信 OTP）
 *   4  多轮重试后仍未进入首页（会话未落地 / 网络 / 上游 502）
 *   1  其他异常
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
  return undefined;
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

// 从卡片祖先链的 React props 中按 DOM 顺序收集去重后的列表数据
async function extractList(page) {
  return page.evaluate(() => {
    const seen = new Set();
    const arr = [];
    const walk = (obj, d) => {
      if (!obj || typeof obj !== 'object' || d > 5) return;
      if (obj.data && typeof obj.data === 'object' && obj.data.url) {
        const dd = obj.data;
        if (dd.id != null && !seen.has(dd.id)) {
          seen.add(dd.id);
          arr.push({ title: dd.title || '', url: dd.url, id: dd.id, date: dd.publisher_date || '' });
        }
      }
      for (const v of Object.values(obj)) walk(v, d + 1);
    };
    for (const el of document.querySelectorAll('a.LpEu0_0e')) {
      let n = el;
      while (n) {
        const k = Object.keys(n).find(x => x.startsWith('__reactProps$'));
        if (k) walk(n[k], 0);
        n = n.parentElement;
      }
    }
    return arr;
  });
}

async function fetchBody(detailPage, wrapped) {
  await detailPage.goto(wrapped, { waitUntil: 'domcontentloaded', timeout: 20000 }).catch(() => {});
  await detailPage.waitForSelector('.wp_articlecontent p, article p', { timeout: 6000 }).catch(() => {});
  return detailPage.evaluate(() => {
    const root = document.querySelector('.wp_articlecontent') || document.querySelector('article');
    if (!root) return '';
    return [...root.querySelectorAll('p')]
      .map(p => (p.innerText || '').trim())
      .filter(t => t && !/浏览次数|发布时间|来源：/.test(t))
      .join(' ')
      .slice(0, 600);
  }).catch(() => '');
}

async function attempt(browser, cred, START, debug) {
  const ctx = await browser.newContext({
    ignoreHTTPSErrors: true,
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
  });
  const page = await ctx.newPage();
  let cards = [], authCode = null;
  try {
    await page.goto(START, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1500);

    const has = async sel => { try { return !!(await page.$(sel)); } catch (e) { return false; } };
    const dismissTrustLayer = async () => {
      for (const sel of ['a.layui-layer-btn0', 'a.layui-layer-btn1']) {
        if (await has(sel)) { await page.click(sel).catch(() => {}); await page.waitForTimeout(500); return; }
      }
    };

    if (await has('.user .userinput input')) {
      authCode = await submitLogin(page, cred, debug);
    }

    for (let i = 0; i < 10; i++) {
      await page.waitForTimeout(3000);
      await dismissTrustLayer();
      cards = await extractList(page);
      if (cards.length > 0) break;
      if (await has('.user .userinput input')) {
        authCode = await submitLogin(page, cred, debug);
      }
    }
    return { ctx, page, cards, authCode };
  } finally {
    if (cards.length === 0) await ctx.close().catch(() => {});
  }
}

async function main() {
  const outFile = process.argv[2];
  if (!outFile) { console.error('usage: node portal_fetch.js <outJsonFile>'); process.exit(1); }

  const cred = loadCred();
  if (!cred.username || !cred.password) process.exit(2);

  let chromium;
  try { ({ chromium } = require('playwright')); }
  catch (e) { console.error('playwright module unavailable'); process.exit(1); }

  const START = process.env.PORTAL_HOME || DEFAULT_START;
  const debug = !!process.env.PORTAL_DEBUG;
  const detailLimit = Math.max(0, parseInt(process.env.PORTAL_DETAIL_LIMIT || '10', 10) || 0);
  const browser = await chromium.launch({
    executablePath: pickBrowser(),
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });

  const ROUNDS = 3;
  let ctx = null, page = null, cards = [];
  try {
    for (let round = 1; round <= ROUNDS; round++) {
      if (debug) console.error(`[dbg] round ${round}`);
      const res = await attempt(browser, cred, START, debug);
      ctx = res.ctx; page = res.page; cards = res.cards;
      if (cards.length > 0) break;
      if (res.authCode !== null && String(res.authCode) !== '100000001') process.exit(3);
      if (round < ROUNDS) await new Promise(r => setTimeout(r, 5000));
    }
    if (cards.length === 0) process.exit(4);

    // 详情正文：仅近期前 N 条；复用独立 detail 页面
    let withBody = 0;
    if (detailLimit > 0) {
      const detail = await ctx.newPage();
      const subset = cards.slice(0, detailLimit);
      for (const item of subset) {
        try {
          const wrapped = await page.evaluate(u => vpn_rewrite_url(u), item.url);
          item.body = await fetchBody(detail, wrapped) || '';
        } catch (e) { item.body = ''; }
        if (item.body) withBody++;
      }
      await detail.close().catch(() => {});
    }
    for (const c of cards) if (!('body' in c)) c.body = '';

    fs.writeFileSync(outFile, JSON.stringify({ cards }), 'utf-8');
    console.log(`RESULT items=${cards.length} detail=${withBody}`);
  } finally {
    if (ctx) await ctx.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

main().catch(e => {
  console.error('portal fetch failed: ' + String(e && e.message || e).slice(0, 160));
  process.exit(1);
});
