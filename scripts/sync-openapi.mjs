// scripts/sync-openapi.mjs
//
// Lấy bản snapshot OpenAPI từ repo backend (Scrap JD) về frontend rồi sinh lại lib/api/types.ts.
//
//   npm run sync:openapi -- "D:/duong/dan/Scrap JD"          # thư mục gốc repo backend
//   npm run sync:openapi -- "D:/duong/dan/openapi.snapshot.json"   # hoặc trỏ thẳng vào file
//   BACKEND_REPO_DIR="D:/duong/dan/Scrap JD" npm run sync:openapi  # hoặc qua biến môi trường
//
// Vì sao copy file thay vì gọi server đang chạy: backend TẮT /openapi.json mặc định (ENABLE_DOCS), và snapshot đã
// commit ở backend (openapi.snapshot.json, có test giữ cho nó khớp API) là nguồn duy nhất, nên mọi máy và CI sinh ra
// cùng một types.ts. Chi tiết ở docstring scrapjd/api/openapi_snapshot.py của backend.
//
// Cờ: --dest <file>   ghi bản copy vào chỗ khác (mặc định openapi/openapi.snapshot.json)
//     --skip-types    chỉ copy, không sinh lại types.ts (dùng trong test)
//
// Thoát mã 0 khi xong, 1 khi lỗi (nguồn thiếu, JSON hỏng, không phải OpenAPI 3.x).

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SNAPSHOT_NAME = "openapi.snapshot.json";
const DEFAULT_DEST = path.join(ROOT, "openapi", SNAPSHOT_NAME);
const TYPES_OUT = path.join(ROOT, "lib", "api", "types.ts");

const HTTP_METHODS = ["get", "put", "post", "delete", "options", "head", "patch", "trace"];

function fail(message) {
  console.error(`Lỗi: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const out = { source: process.env.BACKEND_REPO_DIR || "", dest: DEFAULT_DEST, skipTypes: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--skip-types") out.skipTypes = true;
    else if (arg === "--dest") {
      const value = argv[++i];
      if (!value) fail("--dest cần một đường dẫn.");
      out.dest = path.resolve(value);
    } else if (arg.startsWith("--")) fail(`cờ không hợp lệ: ${arg}`);
    else out.source = arg;
  }
  return out;
}

function resolveSource(source) {
  if (!source) {
    fail(
      "chưa chỉ nguồn. Dùng: npm run sync:openapi -- <thư mục repo backend hoặc file openapi.snapshot.json> " +
        "(hoặc đặt BACKEND_REPO_DIR).",
    );
  }
  const resolved = path.resolve(source);
  if (!existsSync(resolved)) fail(`không thấy ${resolved}`);
  const file = statSync(resolved).isDirectory() ? path.join(resolved, SNAPSHOT_NAME) : resolved;
  if (!existsSync(file)) {
    fail(`không thấy ${file}. Backend đã commit ${SNAPSHOT_NAME} chưa? (python -m scrapjd.api.openapi_snapshot)`);
  }
  return file;
}

/** Đọc + kiểm tra là OpenAPI 3.x có paths. Trả về { text (đã đổi CRLF -> LF), endpoints }. */
function readSnapshot(file) {
  // Đổi CRLF về LF để bản copy giống hệt nhau dù backend nằm trên máy Windows.
  const text = readFileSync(file, "utf8").replace(/\r\n/g, "\n");
  let schema;
  try {
    schema = JSON.parse(text);
  } catch (error) {
    fail(`${file} không phải JSON hợp lệ (${error.message}).`);
  }
  if (typeof schema?.openapi !== "string" || !schema.openapi.startsWith("3.")) {
    fail(`${file} không phải tài liệu OpenAPI 3.x (thiếu "openapi").`);
  }
  if (!schema.paths || typeof schema.paths !== "object") fail(`${file} không có "paths".`);
  let endpoints = 0;
  for (const item of Object.values(schema.paths)) {
    endpoints += HTTP_METHODS.filter((method) => method in item).length;
  }
  if (endpoints === 0) fail(`${file} không có endpoint nào, nghi là file rỗng hoặc sai.`);
  return { text, endpoints };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const { text, endpoints } = readSnapshot(resolveSource(args.source));

  const previous = existsSync(args.dest) ? readFileSync(args.dest, "utf8") : null;
  mkdirSync(path.dirname(args.dest), { recursive: true });
  writeFileSync(args.dest, text, "utf8");
  const status = previous === null ? "tạo mới" : previous === text ? "không đổi" : "ĐÃ ĐỔI";
  console.log(`Snapshot: ${path.relative(ROOT, args.dest) || args.dest} — ${endpoints} endpoint (${status}).`);

  if (args.skipTypes) return;

  // Gọi thẳng cli của openapi-typescript bằng node (không qua npx/.cmd) để chạy được cả trên Windows,
  // và đường dẫn có dấu cách không bị tách sai.
  const cli = path.join(ROOT, "node_modules", "openapi-typescript", "bin", "cli.js");
  if (!existsSync(cli)) fail("chưa cài phụ thuộc. Chạy `npm ci` trước.");
  const result = spawnSync(process.execPath, [cli, args.dest, "-o", TYPES_OUT], { stdio: "inherit" });
  if (result.status !== 0) fail("openapi-typescript báo lỗi, xem log ở trên.");
  console.log("Đã sinh lại lib/api/types.ts. Chạy `npm run typecheck` và `npm test`, rồi commit cả hai file.");
}

main();
