// 前端行为复现脚本：验证探测路径的响应不会让 res.json() 抛异常。
//
// 为什么写这个脚本：
//   用户界面报的错误是
//     Failed to execute 'json' on 'Response': Unexpected end of JSON input
//   这是浏览器原生 fetch 的报错文案。用 curl 只能看到 body 内容，
//   看不出"客户端解析这一步会不会炸"。
//   因此这里用 Node 的 fetch 复刻浏览器完全相同的调用：先 res.json()，
//   再判断是否抛异常。这才是对用户所见错误的真实复现。
//
// 用法：node scripts/verify_probe_response.mjs [base_url]
//   默认 base_url = http://127.0.0.1:8080

const BASE = process.argv[2] || "http://127.0.0.1:8080";

// 会被探测插件扫描的路径，与 app/main.py 的 _PROBE_PATHS 保持一致。
const PROBE_PATHS = [
  "/tools",
  "/props",
  "/props?autoload=false",
  "/build.json",
  "/v1/models",
  "/sw.js",
  "/favicon.ico",
  "/robots.txt",
];

// 需要同时验证非 GET 方法：实机日志里真实出现过 POST /tools。
const POST_PATHS = ["/tools", "/props", "/v1/models"];

let failed = 0;

/** 复刻前端行为：取响应并尝试 res.json()。
 *
 * @param {string} url 完整 URL
 * @param {string} method HTTP 方法
 * @returns {Promise<void>}
 */
async function check(url, method) {
  const label = `${method} ${url.replace(BASE, "")}`;
  try {
    const res = await fetch(url, { method });
    const status = res.status;
    // 关键一步：与浏览器前端完全相同的调用。
    const body = await res.json();
    const okShape =
      body &&
      typeof body === "object" &&
      "ok" in body &&
      "error_type" in body;
    if (!okShape) {
      console.log(`  ✗ ${label}  status=${status}  JSON 结构不符: ${JSON.stringify(body)}`);
      failed += 1;
      return;
    }
    console.log(`  ✓ ${label}  status=${status}  json 可解析  error_type=${body.error_type}`);
  } catch (err) {
    // 这里能捕获到的错误，就是用户在界面上看到的那一个。
    console.log(`  ✗ ${label}  解析失败: ${err.message}`);
    failed += 1;
  }
}

console.log(`验证目标：${BASE}`);
console.log("--- 探测路径（GET）---");
for (const p of PROBE_PATHS) {
  await check(BASE + p, "GET");
}

console.log("--- 探测路径（非 GET，实机日志出现过 POST /tools）---");
for (const p of POST_PATHS) {
  await check(BASE + p, "POST");
}

console.log("--- 真实接口应正常工作 ---");
for (const p of ["/health", "/api/tools/list_pending?limit=5"]) {
  try {
    const res = await fetch(BASE + p);
    const body = await res.json();
    if (res.status !== 200) {
      console.log(`  ✗ GET ${p}  status=${res.status}（应为 200）`);
      failed += 1;
    } else {
      console.log(`  ✓ GET ${p}  status=200  json 可解析`);
    }
  } catch (err) {
    console.log(`  ✗ GET ${p}  失败: ${err.message}`);
    failed += 1;
  }
}

console.log("");
if (failed === 0) {
  console.log("结果：全部通过，前端 res.json() 不会抛 Unexpected end of JSON input");
  process.exit(0);
} else {
  console.log(`结果：${failed} 项失败`);
  process.exit(1);
}
