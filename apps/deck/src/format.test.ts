import { describe, expect, it } from "vitest";
import { reelErrorText } from "./format";

describe("reel errors", () => {
  it("explains known classes in plain German", () => {
    expect(reelErrorText("fetch_login_required")).toContain("Anmeldung");
    expect(reelErrorText("fetch_failed")).toContain("--show-sandbox-errors");
  });

  it("shows unknown classes as they are", () => {
    expect(reelErrorText("something_new")).toBe("something_new");
  });
});
