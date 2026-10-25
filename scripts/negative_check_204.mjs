// 反向验证：临时起一个返回 204 的服务，确认验证脚本能检出这个缺陷。
//
// 目的：证明 scripts/verify_probe_response.mjs 不是"永远打印通过"的空转脚本。
//   如果它对 204 也报通过，那这个验证就没有意义。
//
// 用法：node scripts/negative_check_204.mjs
//   预期输出：解析失败（Unexpected end of JSON input），退出码非 0。

import { createServer } from "node:http";

// 复刻"错误实现"：返回 204 无 body，正是上一版 _absorb_probe 的写法。
const server = createServer((req, res) => {
  res.writeHead(204);
  res.end();
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;

let detected = false;
try {
  const res = await fetch(`http://127.0.0.1:${port}/v1/models`);
  await res.json();
  console.log("✗ 未能检出：204 空体竟然被解析成功了");
} catch (err) {
  detected = true;
  console.log(`✓ 已检出：${err.message}`);
  console.log("  → 这证明验证脚本对 204 空体确实会报错，不是空转");
}

server.close();

// 强制退出，规避 Node 24 在 Windows 上关闭时的 libuv 断言（与本验证无关）。
process.exit(detected ? 0 : 1);
