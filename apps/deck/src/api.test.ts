import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getAttention } from "./api";

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
});
