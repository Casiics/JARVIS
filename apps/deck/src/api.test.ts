import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getAttention, getReels } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(status: number, body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status }))),
  );
}

describe("api", () => {
  it("surfaces gateway errors", async () => {
    stubFetch(401, { detail: "login required" });
    await expect(getAttention()).rejects.toEqual(new ApiError(401, "login required"));
  });

  it("refuses responses that do not match the schema", async () => {
    stubFetch(200, { approvals: "nope", count: 1 });
    await expect(getAttention()).rejects.toThrow();
  });

  it("accepts reel lists and refuses foreign links in them", async () => {
    const reel = {
      task_id: "6f1c1b7e-4b1a-4c55-9d7b-0d9a1c0b8f11",
      reel_code: "ABCDE12345",
      source_ref: "https://www.instagram.com/reel/ABCDE12345/",
      status: "running",
      submitted_at: "2026-10-16T10:00:00+00:00",
      updated_at: "2026-10-16T10:00:05+00:00",
      duration_ms: 5000,
      error_class: null,
    };
    stubFetch(200, { reels: [reel] });
    await expect(getReels()).resolves.toEqual({ reels: [reel] });
    stubFetch(200, { reels: [{ ...reel, source_ref: "https://evil.example/x" }] });
    await expect(getReels()).rejects.toThrow();
  });
});
