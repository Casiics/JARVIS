import { describe, expect, it } from "vitest";
import { filterCommands, isPaletteShortcut, moveSelection, type Command } from "./palette";

const cmd = (id: string, title: string, keywords: string[] = []): Command => ({
  id,
  title,
  hint: "",
  group: "Navigation",
  keywords,
});

const ALL = [
  cmd("a", "Agenten", ["schwarm", "hermes"]),
  cmd("c", "Content", ["reels"]),
  cmd("r", "Fehlgeschlagene Reels erneut versuchen", ["retry"]),
];

describe("filterCommands", () => {
  it("returns everything for an empty query", () => {
    expect(filterCommands(ALL, "  ")).toHaveLength(3);
  });

  it("matches titles and keywords, title hits first, ignoring case and accents", () => {
    expect(filterCommands(ALL, "reels").map((c) => c.id)).toEqual(["r", "c"]);
    expect(filterCommands(ALL, "HERMES").map((c) => c.id)).toEqual(["a"]);
    expect(filterCommands([cmd("p", "Prüfung")], "prufung")).toHaveLength(1);
  });

  it("needs every word to match", () => {
    expect(filterCommands(ALL, "reels retry").map((c) => c.id)).toEqual(["r"]);
    expect(filterCommands(ALL, "reels hermes")).toEqual([]);
  });
});

describe("keyboard", () => {
  it("wraps the selection", () => {
    expect(moveSelection(0, -1, 3)).toBe(2);
    expect(moveSelection(2, 1, 3)).toBe(0);
    expect(moveSelection(0, 1, 0)).toBe(0);
  });

  it("opens on cmd or ctrl plus k", () => {
    expect(isPaletteShortcut({ key: "k", metaKey: true, ctrlKey: false })).toBe(true);
    expect(isPaletteShortcut({ key: "K", metaKey: false, ctrlKey: true })).toBe(true);
    expect(isPaletteShortcut({ key: "k", metaKey: false, ctrlKey: false })).toBe(false);
  });
});
