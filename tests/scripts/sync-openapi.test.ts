import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

// Chạy script THẬT (scripts/sync-openapi.mjs) bằng tiến trình con, luôn kèm --skip-types và --dest trỏ vào thư mục
// tạm để KHÔNG đụng openapi/openapi.snapshot.json và lib/api/types.ts thật.
const SCRIPT = path.resolve(__dirname, "../../scripts/sync-openapi.mjs");

const VALID = {
  openapi: "3.1.0",
  info: { title: "t", version: "1" },
  paths: {
    "/jobs": { get: { operationId: "list_jobs" }, post: { operationId: "create_job" } },
    "/health": { get: { operationId: "health" } },
  },
};

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "sync-openapi-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function run(args: string[], env: Record<string, string> = {}) {
  const result = spawnSync(process.execPath, [SCRIPT, ...args], {
    encoding: "utf8",
    // Xoá BACKEND_REPO_DIR của máy đang chạy để test không phụ thuộc môi trường người dùng.
    env: { ...process.env, BACKEND_REPO_DIR: "", ...env },
  });
  return { code: result.status, out: result.stdout, err: result.stderr };
}

function writeBackend(name: string, content: string): string {
  const repo = path.join(dir, name);
  mkdirSync(repo, { recursive: true });
  writeFileSync(path.join(repo, "openapi.snapshot.json"), content);
  return repo;
}

const dest = () => path.join(dir, "out", "snap.json");
const json = (value: unknown) => JSON.stringify(value, null, 2) + "\n";

describe("sync-openapi: đường vào", () => {
  it("copy từ thư mục repo backend, tạo thư mục đích nếu chưa có", () => {
    const repo = writeBackend("backend", json(VALID));
    const r = run([repo, "--dest", dest(), "--skip-types"]);
    expect(r.code).toBe(0);
    expect(r.out).toContain("3 endpoint");
    expect(r.out).toContain("tạo mới");
    expect(readFileSync(dest(), "utf8")).toBe(json(VALID));
  });

  it("nhận thẳng đường dẫn file", () => {
    const repo = writeBackend("backend", json(VALID));
    const r = run([path.join(repo, "openapi.snapshot.json"), "--dest", dest(), "--skip-types"]);
    expect(r.code).toBe(0);
    expect(existsSync(dest())).toBe(true);
  });

  it("thư mục có dấu cách (như 'Scrap JD') không bị tách sai", () => {
    const repo = writeBackend("Scrap JD", json(VALID));
    const r = run([repo, "--dest", dest(), "--skip-types"]);
    expect(r.code).toBe(0);
    expect(r.out).toContain("3 endpoint");
  });

  it("đọc nguồn từ biến BACKEND_REPO_DIR khi không có đối số", () => {
    const repo = writeBackend("backend", json(VALID));
    const r = run(["--dest", dest(), "--skip-types"], { BACKEND_REPO_DIR: repo });
    expect(r.code).toBe(0);
    expect(existsSync(dest())).toBe(true);
  });

  it("đối số thắng biến môi trường", () => {
    const good = writeBackend("good", json(VALID));
    const r = run([good, "--dest", dest(), "--skip-types"], { BACKEND_REPO_DIR: path.join(dir, "khong-co") });
    expect(r.code).toBe(0);
  });
});

describe("sync-openapi: trạng thái báo cáo", () => {
  it("chạy lại cùng nội dung -> 'không đổi'; nội dung khác -> 'ĐÃ ĐỔI'", () => {
    const repo = writeBackend("backend", json(VALID));
    run([repo, "--dest", dest(), "--skip-types"]);

    expect(run([repo, "--dest", dest(), "--skip-types"]).out).toContain("không đổi");

    writeFileSync(path.join(repo, "openapi.snapshot.json"), json({ ...VALID, info: { title: "khác", version: "2" } }));
    const r = run([repo, "--dest", dest(), "--skip-types"]);
    expect(r.out).toContain("ĐÃ ĐỔI");
    expect(JSON.parse(readFileSync(dest(), "utf8")).info.title).toBe("khác");
  });

  it("đổi CRLF (backend trên Windows) về LF để bản copy giống nhau giữa các máy", () => {
    const repo = writeBackend("backend", json(VALID).replace(/\n/g, "\r\n"));
    expect(run([repo, "--dest", dest(), "--skip-types"]).code).toBe(0);
    const copied = readFileSync(dest(), "utf8");
    expect(copied).not.toContain("\r");
    expect(copied).toBe(json(VALID));
  });

  it("chỉ đếm method HTTP, không đếm khoá khác cùng cấp path (vd parameters)", () => {
    const schema = { ...VALID, paths: { "/a": { get: {}, parameters: [{ name: "x" }], summary: "s" } } };
    const repo = writeBackend("backend", json(schema));
    expect(run([repo, "--dest", dest(), "--skip-types"]).out).toContain("1 endpoint");
  });
});

describe("sync-openapi: lỗi (thoát mã 1, KHÔNG ghi đè bản đang có)", () => {
  function expectFailure(args: string[], message: RegExp, env: Record<string, string> = {}) {
    const before = "BẢN CŨ\n";
    mkdirSync(path.dirname(dest()), { recursive: true });
    writeFileSync(dest(), before);
    const r = run([...args, "--dest", dest(), "--skip-types"], env);
    expect(r.code).toBe(1);
    expect(r.err).toMatch(message);
    expect(readFileSync(dest(), "utf8")).toBe(before);
  }

  it("không chỉ nguồn nào", () => {
    expectFailure([], /chưa chỉ nguồn/);
  });

  it("nguồn không tồn tại", () => {
    expectFailure([path.join(dir, "khong-co")], /không thấy/);
  });

  it("thư mục backend chưa có snapshot -> gợi ý lệnh sinh", () => {
    const empty = path.join(dir, "empty");
    mkdirSync(empty);
    expectFailure([empty], /python -m scrapjd\.api\.openapi_snapshot/);
  });

  it("JSON hỏng", () => {
    expectFailure([writeBackend("b", '{"openapi": "3.1.0", ')], /không phải JSON hợp lệ/);
  });

  it("không phải OpenAPI 3.x (Swagger 2.0 hoặc thiếu khoá openapi)", () => {
    expectFailure([writeBackend("b", json({ swagger: "2.0", paths: { "/a": { get: {} } } }))], /không phải tài liệu OpenAPI 3/);
    expectFailure([writeBackend("c", json({ openapi: "2.0", paths: { "/a": { get: {} } } }))], /không phải tài liệu OpenAPI 3/);
  });

  it("thiếu paths", () => {
    expectFailure([writeBackend("b", json({ openapi: "3.1.0" }))], /không có "paths"/);
  });

  it("paths rỗng (file rỗng/sai) không được chép đè lên bản tốt", () => {
    expectFailure([writeBackend("b", json({ openapi: "3.1.0", paths: {} }))], /không có endpoint nào/);
  });

  it("cờ lạ", () => {
    expectFailure([writeBackend("b", json(VALID)), "--khong-co"], /cờ không hợp lệ/);
  });

  it("--dest thiếu giá trị", () => {
    const r = run([writeBackend("b", json(VALID)), "--dest"]);
    expect(r.code).toBe(1);
    expect(r.err).toContain("--dest cần một đường dẫn");
  });
});
